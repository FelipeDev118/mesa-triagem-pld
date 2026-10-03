"""Passos 0.6 e 0.7 - executa as regras sobre o store e materializa o resultado.

O que muda em relacao a rodar `python nivel_2/dados.py`: nada no CALCULO. As
mesmas funcoes, sobre o mesmo DataFrame. O que muda e que o resultado deixa de
ser um print e vira registro:

  execucoes_regras  - quando rodou, com QUAIS parametros (auditoria)
  sinalizacoes      - cada disparo individual, com o detalhe que o motivou
  alertas           - a fila do analista, um por cliente

`sinalizacoes` e a peca que faltava para o alerta ser explicavel. Hoje
`aplicar_regras()` colapsa o fracionamento num booleano por cliente e a
informacao de QUAL dia disparou tem que ser recalculada depois
(`datas_fracionamento()` roda `flag_fracionamento()` inteira de novo so para
descobrir uma data). Gravando o disparo, isso vira um SELECT.
"""
import json
import sqlite3
import sys
from dataclasses import dataclass

import pandas as pd

import mesa  # noqa: F401  - poe nivel_2/ no sys.path
from confronto import nivel_risco_esperado
from dados import (
    PARAMETROS_REGRAS,
    VERSAO_REGRAS,
    aplicar_regras,
    flag_fracionamento,
    flag_valor_atipico,
    todos_os_clientes,
)
from mesa import repositorio
from mesa.db import agora_utc, conectar


@dataclass
class ResultadoExecucao:
    execucao_id: int
    escopo: str
    operacoes_avaliadas: int
    clientes_fracionamento: int
    operacoes_atipicas: int
    sinalizacoes: int
    alertas_regra: int
    alertas_controle: int

    def __str__(self) -> str:
        return (
            f"execucao {self.execucao_id} ({self.escopo}): {self.operacoes_avaliadas} operacoes | "
            f"{self.clientes_fracionamento} clientes com fracionamento | "
            f"{self.operacoes_atipicas} operacoes atipicas | "
            f"{self.sinalizacoes} sinalizacoes | "
            f"{self.alertas_regra} alertas + {self.alertas_controle} de controle"
        )


def _lote_mais_recente(conn: sqlite3.Connection) -> int:
    linha = conn.execute("SELECT id FROM lotes_ingestao ORDER BY id DESC LIMIT 1").fetchone()
    if linha is None:
        raise RuntimeError("store vazio: rode a ingestao antes de executar as regras")
    return int(linha[0])


def _gravar_sinalizacoes(conn: sqlite3.Connection, execucao_id: int, df: pd.DataFrame,
                         parametros: dict) -> int:
    """Uma linha por disparo. Fracionamento e por (cliente, DIA); valor atipico e
    por OPERACAO - por isso operacao_id fica NULL num caso e data no outro."""
    linhas = []

    for _, c in flag_fracionamento(df, parametros).iterrows():
        linhas.append((
            execucao_id, c["cliente_id"], "fracionamento", None,
            c["data"].strftime("%Y-%m-%d"),
            json.dumps({
                "soma_do_dia": round(float(c["soma"]), 2),
                "qtd_operacoes": int(c["qtd"]),
                "max_individual": round(float(c["max_individual"]), 2),
            }, ensure_ascii=False),
        ))

    atipicos = flag_valor_atipico(df, parametros)
    for _, a in atipicos[atipicos["atipico"]].iterrows():
        linhas.append((
            execucao_id, a["cliente_id"], "valor_atipico", a["id"], None,
            json.dumps({
                "valor_brl": round(float(a["valor_brl"]), 2),
                "limite_atipico": round(float(a["limite_atipico"]), 2),
            }, ensure_ascii=False),
        ))

    conn.executemany(
        "INSERT INTO sinalizacoes (execucao_id, cliente_id, regra, operacao_id, "
        "data, detalhe_json) VALUES (?, ?, ?, ?, ?, ?)",
        linhas,
    )
    return len(linhas)


def _vigentes_por_cliente(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r[0]: r[1] for r in conn.execute(
            f"SELECT a.cliente_id, a.id FROM alertas a WHERE {repositorio.VIGENTE}"
        )
    }


def _gravar_alertas(conn: sqlite3.Connection, execucao_id: int, df_regras: pd.DataFrame) -> tuple[int, int]:
    """Um alerta por cliente da base.

    origem='controle' para quem nao tem nenhuma sinalizacao: a fila do analista
    filtra origem='regra' (chamar de "alerta" um cliente sem sinal seria
    mentira), mas os de controle continuam existindo porque sao eles que medem
    o falso negativo (ver DECISOES.md, "Cobrir os 30 clientes").
    """
    clientes = todos_os_clientes(df_regras)
    agora = agora_utc()
    # cada alerta novo SUBSTITUI o vigente do cliente (Fase 5.1): o antigo sai
    # da fila, mas continua existindo com a decisao que tiver
    vigentes = _vigentes_por_cliente(conn)
    linhas = []
    for _, row in clientes.iterrows():
        frac = int(row["sinalizacoes_fracionamento"])
        atip = int(row["sinalizacoes_valor_atipico"])
        total = int(row["total_sinalizacoes"])
        linhas.append((
            execucao_id, row["cliente_id"], "regra" if total > 0 else "controle",
            frac, atip, total,
            round(float(row["volume_total_brl"]), 2), int(row["qtd_operacoes"]),
            # mesmo criterio do confronto.py, importado e nao reimplementado:
            # se a regra "espera alto" muda de definicao, muda nos dois lugares
            nivel_risco_esperado(bool(frac), atip),
            agora,
            vigentes.get(row["cliente_id"]),
        ))

    conn.executemany(
        "INSERT INTO alertas (execucao_id, cliente_id, origem, "
        "sinalizacoes_fracionamento, sinalizacoes_valor_atipico, total_sinalizacoes, "
        "volume_total_brl, qtd_operacoes, nivel_risco_regra, criado_em, substitui_alerta_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        linhas,
    )
    de_regra = sum(1 for l in linhas if l[2] == "regra")
    return de_regra, len(linhas) - de_regra


def clientes_do_delta(conn: sqlite3.Connection, desde_lote: int) -> set[str]:
    """Clientes com operacao nova ou corrigida depois de `desde_lote`.

    A segunda metade pega a correcao que TIROU a operacao de um cliente (ela
    agora esta em outro): a versao antiga, no historico, guarda o cliente de
    antes - ele tambem mudou de resultado."""
    return {
        r[0] for r in conn.execute(
            "SELECT cliente_id FROM operacoes WHERE lote_id > ? "
            "UNION SELECT cliente_id FROM operacoes_historico WHERE substituida_pelo_lote > ?",
            (desde_lote, desde_lote),
        )
    }


def planejar(conn: sqlite3.Connection, parametros: dict = PARAMETROS_REGRAS) -> tuple[str, set[str] | None]:
    """Decide, pelo store, o que a proxima execucao precisa avaliar.

    ('completa', None)         primeira execucao, ou regras/parametros mudaram
                               desde a ultima - qualquer resultado pode mudar
    ('incremental', {clientes}) so quem teve operacao nova/corrigida
    ('nada', set())            nenhum lote novo mexeu em cliente nenhum
    """
    ultima = conn.execute(
        "SELECT lote_id, versao_regras, parametros_json FROM execucoes_regras "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if ultima is None:
        return "completa", None
    if (ultima["versao_regras"] != VERSAO_REGRAS
            or json.loads(ultima["parametros_json"]) != parametros):
        return "completa", None
    delta = clientes_do_delta(conn, ultima["lote_id"])
    return ("incremental", delta) if delta else ("nada", set())


def executar(conn: sqlite3.Connection, parametros: dict = PARAMETROS_REGRAS,
             forcar_completa: bool = False) -> ResultadoExecucao | None:
    """Roda as regras sobre o que mudou e materializa o resultado.

    Devolve None quando nao ha nada a fazer - nenhuma execucao e gravada. Rodar
    duas vezes seguidas e, por isso, seguro: a segunda nao cria 30 alertas novos
    iguais aos anteriores (o que tiraria da fila o trabalho de todo analista)."""
    df = repositorio.operacoes_df(conn)
    if df.empty:
        raise RuntimeError("store vazio: rode a ingestao antes de executar as regras")

    escopo, delta = ("completa", None) if forcar_completa else planejar(conn, parametros)
    if escopo == "nada":
        return None
    if escopo == "incremental":
        # So vale porque as regras sao POR CLIENTE - o teste
        # test_regras_sobre_o_delta_sao_as_mesmas_da_base_inteira prende isso.
        # Uma regra entre clientes (ex.: contraparte compartilhada) quebraria
        # a igualdade, e ai o delta teria que alargar.
        df = df[df["cliente_id"].isin(delta)]

    df_regras = aplicar_regras(df, parametros)

    clientes_frac = int(df_regras.loc[df_regras["flag_fracionamento"], "cliente_id"].nunique())
    operacoes_atipicas = int(df_regras["flag_valor_atipico"].sum())

    cur = conn.execute(
        "INSERT INTO execucoes_regras (lote_id, versao_regras, parametros_json, "
        "executado_em, operacoes_avaliadas, clientes_fracionamento, operacoes_atipicas, escopo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _lote_mais_recente(conn), VERSAO_REGRAS,
            json.dumps(parametros, sort_keys=True), agora_utc(),
            len(df_regras), clientes_frac, operacoes_atipicas, escopo,
        ),
    )
    execucao_id = cur.lastrowid

    n_sinalizacoes = _gravar_sinalizacoes(conn, execucao_id, df, parametros)
    alertas_regra, alertas_controle = _gravar_alertas(conn, execucao_id, df_regras)
    conn.commit()

    return ResultadoExecucao(
        execucao_id=execucao_id,
        escopo=escopo,
        operacoes_avaliadas=len(df_regras),
        clientes_fracionamento=clientes_frac,
        operacoes_atipicas=operacoes_atipicas,
        sinalizacoes=n_sinalizacoes,
        alertas_regra=alertas_regra,
        alertas_controle=alertas_controle,
    )


def datas_fracionamento_do_store(conn: sqlite3.Connection, cliente_id: str,
                                 execucao_id: int | None = None) -> list[str]:
    """O que datas_fracionamento() calcula, agora como consulta.

    A versao em dados.py precisa reprocessar flag_fracionamento() sobre a base
    inteira para descobrir as datas de UM cliente. Aqui o disparo ja esta
    gravado - e o primeiro dividendo concreto de ter sinalizacoes materializadas.
    """
    if execucao_id is None:
        linha = conn.execute(
            "SELECT id FROM execucoes_regras ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if linha is None:
            return []
        execucao_id = int(linha[0])

    return [
        r[0] for r in conn.execute(
            "SELECT data FROM sinalizacoes WHERE execucao_id = ? AND cliente_id = ? "
            "AND regra = 'fracionamento' ORDER BY data",
            (execucao_id, cliente_id),
        )
    ]


if __name__ == "__main__":
    with conectar() as conn:
        resultado = executar(conn, forcar_completa="--completa" in sys.argv)
        print(resultado or "nada a fazer: nenhum lote novo desde a ultima execucao",
              file=sys.stderr)
