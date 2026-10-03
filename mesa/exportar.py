"""Passo 1.8 - os arquivos de outputs/ deixam de ser a fonte e viram uma VISTA.

Ate aqui, `outputs/pareceres_lote.json` era o registro: quem quisesse saber o que
o agente decidiu abria o arquivo. Agora o registro esta no store, e este modulo
regenera os mesmos arquivos a partir dele.

A inversao importa por um motivo pratico: o arquivo e sobrescrito a cada
execucao e nao guarda historico, entao um parecer antigo desaparecia quando o
lote rodava de novo. O store guarda todas as versoes; o arquivo passa a ser um
recorte do estado atual - descartavel e regeneravel, que e o que um relatorio
deve ser.

Os formatos sao EXATAMENTE os que lote.py produzia. Nao por nostalgia: e o que
permite comparar a saida do store com os arquivos commitados da entrega e
verificar que a migracao nao mudou nenhum numero.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

import mesa  # noqa: F401
from observabilidade import ChamadaLLM, Coletor

from mesa.db import RAIZ, conectar

OUTPUTS_DIR = RAIZ / "outputs"


def _pareceres_do_estado_atual(conn: sqlite3.Connection, execucao_id: int | None) -> list[sqlite3.Row]:
    """O parecer mais recente de cada alerta, na ordem da fila: dos alertas
    VIGENTES (padrao, Fase 5.1) ou dos de uma execucao."""
    from mesa.repositorio import VIGENTE

    escopo, params = (VIGENTE, ()) if execucao_id is None else ("a.execucao_id = ?", (execucao_id,))
    return conn.execute(
        f"""
        SELECT a.id AS alerta_id, a.execucao_id, a.cliente_id, a.volume_total_brl,
               a.total_sinalizacoes, a.sinalizacoes_fracionamento,
               a.sinalizacoes_valor_atipico, a.nivel_risco_regra,
               p.id AS parecer_id, p.nivel_risco, p.tipologia_suspeita,
               p.red_flags_json, p.justificativa, p.erro_parsing, p.texto_bruto,
               p.tokens_total, p.latencia_s, p.hash_entrada, p.reaproveitado_de
        FROM alertas a
        LEFT JOIN pareceres p ON p.id = (
            SELECT id FROM pareceres WHERE alerta_id = a.id
            ORDER BY criado_em DESC, id DESC LIMIT 1
        )
        WHERE {escopo}
        ORDER BY a.total_sinalizacoes DESC, a.volume_total_brl DESC, a.cliente_id ASC
        """,
        params,
    ).fetchall()


def _flags(conn: sqlite3.Connection, linha: sqlite3.Row) -> dict:
    from mesa import regras_run

    flags = {
        "flag_fracionamento": bool(linha["sinalizacoes_fracionamento"]),
        "flag_valor_atipico": bool(linha["sinalizacoes_valor_atipico"] > 0),
    }
    if flags["flag_fracionamento"]:
        flags["datas_fracionamento"] = regras_run.datas_fracionamento_do_store(
            conn, linha["cliente_id"], linha["execucao_id"]  # a do proprio alerta
        )
    return flags


def exportar(conn: sqlite3.Connection, execucao_id: int | None = None,
             destino: Path = OUTPUTS_DIR) -> dict:
    destino.mkdir(parents=True, exist_ok=True)
    linhas = _pareceres_do_estado_atual(conn, execucao_id)

    resultados = []
    for linha in linhas:
        parecer = None
        if linha["nivel_risco"] is not None:
            parecer = {
                "nivel_risco": linha["nivel_risco"],
                "tipologia_suspeita": linha["tipologia_suspeita"],
                "red_flags": json.loads(linha["red_flags_json"] or "[]"),
                "justificativa": linha["justificativa"],
            }

        aderencia = conn.execute(
            "SELECT * FROM aderencia WHERE parecer_id = ?", (linha["parecer_id"],)
        ).fetchone() if linha["parecer_id"] else None

        resultados.append({
            "cliente_id": linha["cliente_id"],
            "parecer": parecer,
            "erro_parsing": linha["erro_parsing"],
            "texto_bruto": linha["texto_bruto"],
            "tools_chamadas": _evidencias(conn, linha["parecer_id"]),
            "tokens_total": linha["tokens_total"],
            "latencia_s": linha["latencia_s"],
            # reaproveitado_de preenchido = veio de um parecer anterior, sem
            # chamada de API: e o mesmo significado do antigo cache_hit
            "cache_hit": linha["reaproveitado_de"] is not None,
            "hash_entrada": linha["hash_entrada"],
            "flags_deterministicas": _flags(conn, linha),
            "volume_total_brl": linha["volume_total_brl"],
            "total_sinalizacoes_deterministicas": linha["total_sinalizacoes"],
            "aderencia": {
                "fundamentado": bool(aderencia["fundamentado"]),
                "motivo": aderencia["motivo"],
                "valores_confirmados": json.loads(aderencia["valores_confirmados_json"]),
                "valores_nao_encontrados": json.loads(aderencia["valores_nao_encontrados_json"]),
                "atipicos_incorretos": json.loads(aderencia["atipicos_incorretos_json"]),
            } if aderencia else {"fundamentado": False, "motivo": "sem parecer para verificar"},
        })

    (destino / "pareceres_lote.json").write_text(
        json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    pd.DataFrame([
        {
            "cliente_id": r["cliente_id"],
            "nivel_risco": (r["parecer"] or {}).get("nivel_risco"),
            "erro_parsing": r["erro_parsing"],
            "qtd_tools_chamadas": len(r["tools_chamadas"]),
            "tokens_total": r["tokens_total"],
            "latencia_s": r["latencia_s"],
        }
        for r in resultados
    ]).to_csv(destino / "metricas_lote.csv", index=False)

    pd.DataFrame([
        {"cliente_id": r["cliente_id"], **r["aderencia"]} for r in resultados
    ]).to_csv(destino / "aderencia_pareceres.csv", index=False)

    resumo_custo = _exportar_custo(conn, destino)

    fundamentados = sum(1 for r in resultados if r["aderencia"]["fundamentado"])
    return {
        "execucao_id": execucao_id,
        "clientes": len(resultados),
        "com_parecer": sum(1 for r in resultados if r["parecer"]),
        "fundamentados": fundamentados,
        "chamadas_api": resumo_custo["chamadas_api"],
        "custo_total_usd": resumo_custo["custo_total_usd"],
    }


def _evidencias(conn: sqlite3.Connection, parecer_id: int | None) -> list[dict]:
    if parecer_id is None:
        return []
    chamadas = []
    for linha in conn.execute(
        "SELECT tool, args_json, payload_json FROM evidencias WHERE parecer_id = ? "
        "ORDER BY ordem", (parecer_id,)
    ):
        chamada = {"tool": linha["tool"], "args": json.loads(linha["args_json"])}
        payload = json.loads(linha["payload_json"])
        if payload is not None:
            chamada["payload"] = payload
        chamadas.append(chamada)
    return chamadas


def _exportar_custo(conn: sqlite3.Connection, destino: Path) -> dict:
    """Reconstroi o Coletor de observabilidade.py a partir das linhas gravadas e
    reusa o `resumo()` dele - a agregacao de custo continua definida num lugar so.
    Reimplementar as mesmas medias em SQL criaria uma segunda versao do numero,
    que e como duas verdades comecam.

    Store SEM linha de custo nao sobrescreve os arquivos existentes. Isso nao e
    zelo excessivo: uma triagem 100% reaproveitada do cache importado faz zero
    chamada de API - corretamente - e o cache antigo nunca guardou custo por
    chamada. Exportar "custo zero" por cima de uma medicao real apagaria a
    evidencia de quanto a execucao original custou, que e irrecuperavel. Faltar
    dado no store e motivo para NAO escrever, nao para escrever vazio.
    """
    linhas = conn.execute(
        "SELECT cliente_id, turno, tipo_turno, modelo, tokens_entrada, tokens_saida, "
        "tokens_total, latencia_s, custo_usd, transporte, tentativas_rate_limit "
        "FROM chamadas_llm"
    ).fetchall()

    if not linhas:
        existentes = [
            nome for nome in ("chamadas_llm.csv", "custo_resumo.json")
            if (destino / nome).exists()
        ]
        if existentes:
            print(f"  aviso: store sem custo por chamada; preservando {', '.join(existentes)} "
                  "(exportar zeros apagaria medicao real)")
            return Coletor().resumo()

    coletor = Coletor(chamadas=[ChamadaLLM(**dict(linha)) for linha in linhas])
    coletor.para_dataframe().to_csv(destino / "chamadas_llm.csv", index=False)

    resumo = coletor.resumo()
    (destino / "custo_resumo.json").write_text(
        json.dumps(resumo, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return resumo


if __name__ == "__main__":
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else OUTPUTS_DIR
    with conectar() as conn:
        r = exportar(conn, destino=destino)
    escopo = "alertas vigentes" if r["execucao_id"] is None else f"execucao {r['execucao_id']}"
    print(f"{escopo}: {r['clientes']} clientes | "
          f"{r['com_parecer']} com parecer | {r['fundamentados']} fundamentados | "
          f"{r['chamadas_api']} chamadas de API registradas")
    print(f"exportado para {destino}")
