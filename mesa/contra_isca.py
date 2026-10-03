"""Fase 6.1 / 6.5 - contra-isca: o que passou AO LADO de um caso chamativo.

As regras olham UM cliente por vez. Fracionamento espalhado por 8 clientes
deixa cada um abaixo do limite, e nenhuma regra dispara - por construcao. Esta
e uma CONSULTA entre clientes, sob demanda, a partir de um caso: dado o alerta
(a "isca"), quais operacoes de OUTROS clientes estao ligadas a ele?

Ligacao = operacao de outro cliente com uma contraparte que o caso tambem usa,
e que esta perto dele no tempo de um destes dois jeitos:

  A  na JANELA DE DIAS: ate `janela_dias` antes/depois de uma operacao do
     caso com aquela contraparte (fracionamento distribuido)
  B  num LOTE QUE CHEGOU ENQUANTO O CASO ESTAVA EM ANALISE, pela trilha da
     Fase 4, e com data de operacao DENTRO do periodo (ate janela_dias antes
     do inicio): o que PASSOU enquanto a atencao estava na isca. Uma operacao
     de abril que so chega num lote de junho nao passou "enquanto olhavamos" -
     achado no cenario plantado, onde o historico de um cliente novo chegava
     inteiro no lote da analise e subia acima das plantadas.

Cada ligacao tem um escore = peso da contraparte x soma dos componentes, e
CADA numero vem com a frase de onde saiu (principio 2: nada na tela sem
procedencia):

  peso da contraparte  1 / (1 + outros clientes que a usam FORA da ligacao).
                       Nao "quantos clientes a usam no total": o fracionamento
                       distribuido usa a mesma contraparte em muitos clientes,
                       mas SO na janela - contar o total puniria exatamente o
                       padrao que se procura. A distribuidora de energia de todo
                       mundo aparece o ano inteiro; ela sim pesa pouco.
  janela               1 no mesmo dia, caindo ate 0 depois de janela_dias
  faixa                1 se o valor esta logo abaixo do limite individual do
                       fracionamento - o limite GRAVADO na execucao do alerta,
                       nunca um numero fixo aqui
  distribuicao         quantos clientes diferentes formam o MESMO padrao com a
                       contraparte: no A, os que tem operacao na janela E na
                       faixa; no B, os que chegaram durante a analise. 1 cliente
                       e coincidencia; 4 ou mais e padrao. Contar qualquer
                       ligacao fazia 3 clientes reais que usam a mesma
                       contraparte num mes qualquer parecerem fracionamento
  analise              1 se chegou durante a analise (padrao B)
  volume               so com `analise`: valor / soma minima do fracionamento,
                       ate 1 - no padrao B o que pesa e o volume, nao ficar
                       abaixo de limite

Deterministico, em pandas, sem LLM (principio 4) - e por isso a caca nao manda
dado nenhum para fora. E nao escreve nada: registrar suspeita e decisao do
analista (POST /suspeitas), nunca efeito colateral de olhar.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

# Parametros DA CACA, nao das regras: os limites de valor vem da execucao do
# alerta (execucoes_regras.parametros_json). Mudou algum destes, muda a versao -
# uma suspeita registrada guarda a versao com que a ligacao foi achada.
VERSAO_CACA = "c1-2026-10-03"
PARAMETROS_CACA = {
    "janela_dias": 7,
    "faixa_abaixo_do_limite": 0.85,   # [85%, 100%) do limite individual
    "clientes_para_distribuicao": 4,  # a partir de quantos clientes vale 1
}


def _brl(valor: float) -> str:
    inteiro, centavos = f"{valor:,.2f}".split(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"


def _pct(fracao: float) -> str:
    return f"{fracao * 100:.0f}%"


@dataclass
class _Contexto:
    alerta_id: int
    cliente: str
    execucao_id: int
    limite_individual: float
    soma_minima: float
    periodos: list[tuple[str, str | None, str]]   # (inicio, fim|None, analista)
    lotes_na_analise: dict[int, tuple[str, tuple]]  # lote -> (ingerido_em, periodo)


def periodos_em_analise(trilha: list) -> list[tuple[str, str | None, str]]:
    """(inicio, fim, analista) de cada periodo em em_analise, pela trilha.

    Periodo aberto (o caso continua em analise) tem fim None: o que esta
    passando AGORA e justamente o mais urgente. Saida sem entrada (caso pego
    antes de a trilha existir, esquema v5) nao vira periodo - inventar o inicio
    seria pior que nao ter."""
    periodos, aberto = [], None
    for t in trilha:
        if t["estado_novo"] == "em_analise":
            aberto = (t["registrado_em"], t["ator"])
        elif t["estado_anterior"] == "em_analise" and aberto is not None:
            periodos.append((aberto[0], t["registrado_em"], aberto[1]))
            aberto = None
    if aberto is not None:
        periodos.append((aberto[0], None, aberto[1]))
    return periodos


def _contexto(conn: sqlite3.Connection, alerta_id: int) -> _Contexto:
    alerta = conn.execute(
        "SELECT a.cliente_id, a.execucao_id, e.parametros_json FROM alertas a "
        "JOIN execucoes_regras e ON e.id = a.execucao_id WHERE a.id = ?",
        (alerta_id,),
    ).fetchone()
    if alerta is None:
        raise LookupError(f"alerta {alerta_id} não existe")
    parametros = json.loads(alerta["parametros_json"])
    trilha = conn.execute(
        "SELECT estado_anterior, estado_novo, ator, registrado_em FROM transicoes "
        "WHERE alerta_id = ? ORDER BY id", (alerta_id,),
    ).fetchall()
    periodos = periodos_em_analise(trilha)
    lotes = {}
    for lote_id, ingerido_em in conn.execute("SELECT id, ingerido_em FROM lotes_ingestao"):
        for p in periodos:
            if p[0] <= ingerido_em and (p[1] is None or ingerido_em <= p[1]):
                lotes[lote_id] = (ingerido_em, p)
                break
    return _Contexto(
        alerta_id=alerta_id, cliente=alerta["cliente_id"], execucao_id=alerta["execucao_id"],
        limite_individual=float(parametros["frac_max_individual"]),
        soma_minima=float(parametros["frac_soma_min"]),
        periodos=periodos, lotes_na_analise=lotes,
    )


def _passou_na_analise(linha, ctx: _Contexto, janela: int) -> bool:
    """Chegou num lote ingerido durante a analise E aconteceu nesse periodo -
    data entre (inicio - janela_dias) e o fim (ou sem limite, periodo aberto)."""
    lote = ctx.lotes_na_analise.get(int(linha["lote_id"]))
    if lote is None or pd.isna(linha["data"]):
        return False
    inicio, fim, _ = lote[1]
    dia = linha["data"].date()
    desde = date.fromisoformat(inicio[:10]) - timedelta(days=janela)
    return desde <= dia and (fim is None or dia <= date.fromisoformat(fim[:10]))


def _operacoes(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT id, lote_id, cliente_id, data, valor_brl, contraparte FROM operacoes ORDER BY rowid",
        conn,
    )
    df["data"] = pd.to_datetime(df["data"], format="%Y-%m-%d", errors="coerce")
    return df


def cacar(conn: sqlite3.Connection, alerta_id: int, parametros: dict = PARAMETROS_CACA) -> dict:
    """As ligacoes do caso, da mais forte para a mais fraca. Nao grava nada."""
    ctx = _contexto(conn, alerta_id)
    df = _operacoes(conn)
    do_caso = df[df["cliente_id"] == ctx.cliente]
    outros = df[(df["cliente_id"] != ctx.cliente) & df["contraparte"].isin(set(do_caso["contraparte"]))].copy()

    janela = parametros["janela_dias"]
    # ancoras: as datas das operacoes DO CASO com cada contraparte
    ancoras = {cp: sorted(g["data"].dropna()) for cp, g in do_caso.groupby("contraparte")}

    def mais_proxima(linha):
        datas = ancoras.get(linha["contraparte"], [])
        if pd.isna(linha["data"]) or not datas:
            return None, None
        ancora = min(datas, key=lambda d: abs((linha["data"] - d).days))
        return ancora, abs((linha["data"] - ancora).days)

    proximas = outros.apply(mais_proxima, axis=1, result_type="expand") if len(outros) else None
    outros["ancora"] = proximas[0] if proximas is not None else None
    outros["dias"] = proximas[1] if proximas is not None else None
    outros["na_janela"] = outros["dias"].notna() & (outros["dias"] <= janela)
    outros["na_analise"] = outros.apply(lambda l: _passou_na_analise(l, ctx, janela), axis=1) \
        if len(outros) else False
    outros["ligada"] = outros["na_janela"] | outros["na_analise"]
    piso = parametros["faixa_abaixo_do_limite"] * ctx.limite_individual
    outros["na_faixa"] = (outros["valor_brl"] >= piso) & (outros["valor_brl"] < ctx.limite_individual)

    ligadas = outros[outros["ligada"]]
    # outros clientes que usam a contraparte FORA de qualquer ligacao
    fora = outros[~outros["ligada"]].groupby("contraparte")["cliente_id"].nunique()
    dist_a = ligadas[ligadas["na_janela"] & ligadas["na_faixa"]].groupby("contraparte")["cliente_id"].nunique()
    dist_b = ligadas[ligadas["na_analise"]].groupby("contraparte")["cliente_id"].nunique()

    ligacoes = []
    for _, op in ligadas.iterrows():
        cp = op["contraparte"]
        n_fora = int(fora.get(cp, 0))
        n_a = int(dist_a.get(cp, 0)) if op["na_janela"] else 0
        n_b = int(dist_b.get(cp, 0)) if op["na_analise"] else 0
        n_dist = max(n_a, n_b)
        peso = 1 / (1 + n_fora)
        componentes = [{
            "nome": "contraparte", "valor": round(peso, 4),
            "procedencia": (
                f"contraparte '{cp}', usada pelo caso {ctx.cliente}; fora desta ligação, "
                + ("nenhum outro cliente a usa" if n_fora == 0 else
                   f"{n_fora} outro(s) cliente(s) a usam - peso 1/{1 + n_fora}")
            ),
        }]
        if op["na_janela"]:
            dias = int(op["dias"])
            valor = 1 - dias / (janela + 1)
            quando = ("no mesmo dia da" if dias == 0 else
                      f"{dias} dia(s) {'depois' if op['data'] > op['ancora'] else 'antes'} da")
            componentes.append({
                "nome": "janela", "valor": round(valor, 4),
                "procedencia": f"{quando} operação do caso com '{cp}' em "
                               f"{op['ancora']:%Y-%m-%d} (janela de ±{janela} dias)",
            })
        v = float(op["valor_brl"])
        if op["na_faixa"]:
            componentes.append({
                "nome": "faixa", "valor": 1.0,
                "procedencia": f"{_brl(v)} = {_pct(v / ctx.limite_individual)} do limite individual "
                               f"de {_brl(ctx.limite_individual)} (frac_max_individual da "
                               f"execução {ctx.execucao_id})",
            })
        alvo = parametros["clientes_para_distribuicao"]
        if n_dist >= 2:
            componentes.append({
                "nome": "distribuicao", "valor": round(min(1.0, (n_dist - 1) / (alvo - 1)), 4),
                "procedencia": f"a mesma contraparte liga {n_dist} clientes diferentes ao caso "
                               + ("na janela e logo abaixo do limite" if n_a >= n_b
                                  else "durante a análise"),
            })
        if op["na_analise"]:
            ingerido_em, (inicio, fim, analista) = ctx.lotes_na_analise[int(op["lote_id"])]
            componentes.append({
                "nome": "analise", "valor": 1.0,
                "procedencia": f"operação de {op['data']:%Y-%m-%d}, chegou no lote "
                               f"{int(op['lote_id'])} (ingerido em {ingerido_em}) enquanto o "
                               f"caso estava em análise com {analista} ({inicio} → {fim or 'agora'})",
            })
            componentes.append({
                "nome": "volume", "valor": round(min(1.0, v / ctx.soma_minima), 4),
                "procedencia": f"{_brl(v)} = {v / ctx.soma_minima:.1f}× a soma mínima do "
                               f"fracionamento ({_brl(ctx.soma_minima)}, frac_soma_min da "
                               f"execução {ctx.execucao_id})",
            })
        soma = sum(c["valor"] for c in componentes[1:])
        padroes = [p for p, sim in (("A", op["na_janela"]), ("B", op["na_analise"])) if sim]
        ligacoes.append({
            "operacao_id": op["id"], "cliente_id": op["cliente_id"],
            "data": None if pd.isna(op["data"]) else f"{op['data']:%Y-%m-%d}",
            "valor_brl": round(v, 2), "contraparte": cp, "lote_id": int(op["lote_id"]),
            "padroes": padroes, "escore": round(peso * soma, 4), "componentes": componentes,
        })

    # desempate estavel: escore, depois id - a mesma consulta, a mesma ordem
    ligacoes.sort(key=lambda l: (-l["escore"], l["operacao_id"]))
    # o alerta VIGENTE de cada cliente ligado: e por ele que a tela abre o caso
    from mesa.repositorio import VIGENTE
    vigentes = dict(conn.execute(f"SELECT a.cliente_id, a.id FROM alertas a WHERE {VIGENTE}").fetchall())
    for l in ligacoes:
        l["alerta_do_cliente"] = vigentes.get(l["cliente_id"])
    return {
        "alerta_id": alerta_id,
        "cliente_id": ctx.cliente,
        "execucao_id": ctx.execucao_id,
        "versao_caca": VERSAO_CACA,
        "parametros": dict(parametros),
        "limites": {"frac_max_individual": ctx.limite_individual, "frac_soma_min": ctx.soma_minima},
        "periodos_em_analise": [
            {"inicio": i, "fim": f, "analista": a} for i, f, a in ctx.periodos
        ],
        "ligacoes": ligacoes,
    }
