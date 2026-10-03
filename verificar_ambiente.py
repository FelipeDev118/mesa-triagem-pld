"""Verificacao de reprodutibilidade - roda sem chave de API e sem chamar LLM.

Por que este script existe: o problema do "so funciona na minha maquina" nao se resolve
provando que o codigo *executa* em outro lugar, e sim provando que ele chega ao *mesmo
resultado*. Aqui isso e possivel para a camada deterministica - e so para ela.

O que e verificavel:
  - limpeza dos dados (duplicatas, datas nulas, conversao de moeda)
  - Regra 1 e Regra 2
  - ranking dos 10 clientes mais sinalizados

O que NAO e verificavel, e o motivo importa: os pareceres do LLM nao sao reproduziveis
nem na mesma maquina. O mesmo agente atribuiu nivel_risco diferente para 4 de 10 clientes
entre duas execucoes (ver docs/ARQUITETURA.md). Entao comparar parecer contra parecer
seria um teste que falha por motivo errado. Este script compara o que deve ser identico
em qualquer maquina, e reporta o resto como informativo.

Rodar:
    python verificar_ambiente.py          # local, lendo o JSON
    python verificar_ambiente.py --store  # lendo o store da Mesa (outputs/mesa.db)
    docker compose run --rm verificar     # no container

O modo --store existe porque a Fase 0 do ROADMAP move a base do JSON para
SQLite, e uma migracao de armazenamento so pode ser declarada correta se o novo
caminho produzir os MESMOS numeros que o antigo. Aqui os dois modos comparam
contra o mesmo ESPERADO, entao "o store reproduz a entrega" deixa de ser
afirmacao e vira comando que passa ou falha.
"""
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "nivel_2"))

import pandas as pd

from dados import (
    PARAMETROS_REGRAS,
    aplicar_regras,
    carregar_e_limpar,
    ranking_clientes_sinalizados,
)

# Valores obtidos na execucao que gerou os arquivos em outputs/. Se a mesma base produzir
# numeros diferentes em outra maquina, algo mudou no ambiente (versao de pandas, encoding,
# arredondamento) e o resto da entrega precisa ser lido com desconfianca.
ESPERADO = {
    "operacoes_brutas": 322,
    "duplicatas_removidas": 5,
    "datas_nulas": 7,
    "operacoes_usd": 7,
    "operacoes_apos_limpeza": 317,
    "clientes_fracionamento": 4,  # CLI-002, CLI-003, CLI-017, CLI-029
    "operacoes_atipicas": 21,     # distribuidas em 13 clientes
    "top10": [
        "CLI-014", "CLI-023", "CLI-028", "CLI-013", "CLI-005",
        "CLI-026", "CLI-001", "CLI-029", "CLI-017", "CLI-030",
    ],
}


def obter_do_json() -> tuple[dict, float]:
    bruto = json.loads((RAIZ / "dados" / "dados_nivel_2.json").read_text(encoding="utf-8"))
    df_bruto = pd.DataFrame(bruto["operacoes"])

    df, taxa = carregar_e_limpar()
    df = aplicar_regras(df)
    top10 = ranking_clientes_sinalizados(df, top_n=10)

    return {
        "operacoes_brutas": len(df_bruto),
        "duplicatas_removidas": int(df_bruto["id"].duplicated().sum()),
        "datas_nulas": int(df_bruto["data"].isna().sum()),
        "operacoes_usd": int((df_bruto["moeda"] == "USD").sum()),
        "operacoes_apos_limpeza": len(df),
        "clientes_fracionamento": int(df.loc[df["flag_fracionamento"], "cliente_id"].nunique()),
        "operacoes_atipicas": int(df["flag_valor_atipico"].sum()),
        "top10": top10["cliente_id"].tolist(),
    }, taxa


def obter_do_store() -> tuple[dict, float]:
    """Le os mesmos 8 numeros do banco da Mesa, sem tocar no JSON.

    Nada aqui recalcula regra: se este modo reprocessasse a base, provaria
    apenas que o codigo concorda consigo mesmo. O que ele verifica e o que ficou
    GRAVADO - que e o que a Fase 1 em diante vai consumir.
    """
    from mesa.db import CAMINHO_PADRAO, abrir_para_leitura

    if not CAMINHO_PADRAO.exists():
        raise SystemExit(
            f"store nao encontrado em {CAMINHO_PADRAO}\n"
            "  rode:  python -m mesa.ingestao && python -m mesa.regras_run"
        )

    # so leitura: nao migra o store (ver mesa.db.abrir_para_leitura)
    try:
        conn = abrir_para_leitura(CAMINHO_PADRAO)
    except RuntimeError as erro:
        raise SystemExit(str(erro))

    # Somente os lotes que de fato trouxeram operacao: reingerir o mesmo arquivo
    # cria um lote com 0 inseridas, e conta-lo inflaria os numeros brutos.
    lote = conn.execute(
        "SELECT SUM(operacoes_brutas) ob, SUM(duplicatas_ignoradas) di, "
        "SUM(datas_nulas_brutas) dn, SUM(operacoes_usd_brutas) ou, "
        "MAX(taxa_cambio_usd_brl) taxa FROM lotes_ingestao "
        "WHERE id IN (SELECT DISTINCT lote_id FROM operacoes)"
    ).fetchone()
    if lote["ob"] is None:
        raise SystemExit("store sem operacoes - rode: python -m mesa.ingestao")

    execucao = conn.execute(
        "SELECT * FROM execucoes_regras ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if execucao is None:
        raise SystemExit("store sem execucao de regras - rode: python -m mesa.regras_run")

    # A comparacao contra ESPERADO so faz sentido sob os parametros da entrega.
    # Uma execucao com limiar diferente daria numeros legitimamente diferentes, e
    # reprovar por isso seria falhar pelo motivo errado.
    parametros = json.loads(execucao["parametros_json"])
    if parametros != PARAMETROS_REGRAS:
        raise SystemExit(
            f"a ultima execucao de regras usou parametros diferentes dos da entrega:\n"
            f"  gravado: {parametros}\n  entrega: {PARAMETROS_REGRAS}\n"
            "  rode 'python -m mesa.regras_run' com os parametros padrao antes de verificar"
        )

    # Sobre os alertas VIGENTES, nao sobre a ultima execucao: desde a Fase 5.1
    # ela pode ser incremental e cobrir so os clientes que mudaram - os numeros
    # sairiam do delta como se fossem da base inteira.
    from mesa.repositorio import VIGENTE

    top10 = [
        r[0] for r in conn.execute(
            f"SELECT cliente_id FROM alertas a WHERE {VIGENTE} AND origem = 'regra' "
            "ORDER BY total_sinalizacoes DESC, volume_total_brl DESC, cliente_id ASC LIMIT 10"
        )
    ]
    vigentes = conn.execute(
        f"SELECT SUM(sinalizacoes_fracionamento > 0) frac, SUM(sinalizacoes_valor_atipico) atip "
        f"FROM alertas a WHERE {VIGENTE}"
    ).fetchone()

    obtido = {
        "operacoes_brutas": int(lote["ob"]),
        "duplicatas_removidas": int(lote["di"]),
        "datas_nulas": int(lote["dn"]),
        "operacoes_usd": int(lote["ou"]),
        "operacoes_apos_limpeza": conn.execute(
            "SELECT COUNT(*) FROM operacoes").fetchone()[0],
        "clientes_fracionamento": int(vigentes["frac"]),
        "operacoes_atipicas": int(vigentes["atip"]),
        "top10": top10,
    }
    taxa = float(lote["taxa"])
    conn.close()
    return obtido, taxa


def main(usar_store: bool = False) -> int:
    print(f"pandas {pd.__version__} | python {sys.version.split()[0]}")
    fonte = "store da Mesa (outputs/mesa.db)" if usar_store else "dados/dados_nivel_2.json"
    print(f"fonte: {fonte}\n")

    obtido, taxa = obter_do_store() if usar_store else obter_do_json()

    print(f"{'verificacao':28} {'esperado':>12} {'obtido':>12}   ")
    falhas = []
    for chave, esperado in ESPERADO.items():
        if chave == "top10":
            continue
        ok = obtido[chave] == esperado
        if not ok:
            falhas.append(chave)
        print(f"{chave:28} {esperado:>12} {obtido[chave]:>12}   {'OK' if ok else 'FALHOU'}")

    ok_top = obtido["top10"] == ESPERADO["top10"]
    if not ok_top:
        falhas.append("top10")
    print(f"\ntop 10 clientes sinalizados: {'OK' if ok_top else 'FALHOU'}")
    print(f"  esperado: {ESPERADO['top10']}")
    print(f"  obtido:   {obtido['top10']}")
    print(f"\ntaxa de cambio: {taxa}")

    # Informativo: nao entra no criterio de sucesso, porque parecer de LLM nao e reproduzivel.
    caminho_lote = RAIZ / "outputs" / "pareceres_lote.json"
    if caminho_lote.exists():
        lote = json.loads(caminho_lote.read_text(encoding="utf-8"))
        riscos = pd.Series(
            [(r.get("parecer") or {}).get("nivel_risco") for r in lote]
        ).value_counts().to_dict()
        print(f"\n[informativo] pareceres commitados em outputs/: {len(lote)} | "
              f"distribuicao de nivel_risco: {riscos}")
        print("  (nao verificado: resposta de LLM nao e reproduzivel entre execucoes)")

    if falhas:
        print(f"\nFALHOU em: {', '.join(falhas)}")
        print("A camada deterministica divergiu - investigar antes de confiar no resto.")
        return 1

    print("\nOK: a camada deterministica reproduz exatamente os numeros da entrega.")
    return 0


if __name__ == "__main__":
    sys.exit(main(usar_store="--store" in sys.argv))
