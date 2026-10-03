"""Fase 4.2 e 4.3 - o que as decisoes dos analistas dizem sobre o agente.

A metrica da entrega (regra-vs-agente, 76,67%) compara duas maquinas: ela diz
se o agente reproduz a tabela de risco das regras, nao se ele acerta. Com
decisao humana gravada, a pergunta certa passa a ser respondivel: o agente
chega ao que o ANALISTA decide?

Tudo aqui e contagem sobre o que esta gravado - decisoes, aderencia, trilha.
Nada e estimado, com uma excecao declarada: a economia de tempo (4.3), que so
existe se quem pergunta informar a linha de base, e volta marcada como tal.

Regra que vale para todo numero deste modulo: denominador zero da None, nunca
0.0. "Nenhum caso decidido" e "0% de concordancia" sao afirmacoes diferentes -
a mesma distincao que a fila faz entre "nao triado" e "diverge".
"""
import sqlite3
import statistics
from datetime import datetime

import mesa  # noqa: F401  - poe nivel_2/ no sys.path
from confronto import _normalizar_nivel

NIVEIS = ("baixo", "médio", "alto")
DECISOES = ("concordo", "discordo", "escalar")
PROCEDENCIA_LINHA_DE_BASE = (
    "informada na requisição, não medida - o store não tem o tempo de análise "
    "sem a ferramenta"
)


def _fracao(parte: int, todo: int) -> float | None:
    return None if todo == 0 else parte / todo


def _matriz_vazia() -> dict[str, dict[str, int]]:
    return {linha: {coluna: 0 for coluna in NIVEIS} for linha in NIVEIS}


def _comparacao(pares: list[tuple[str | None, str | None]]) -> dict:
    """Concordancia de nivel entre duas fontes, so sobre os pares em que as
    duas tem nivel. A matriz e [fonte_1][fonte_2], sempre 3x3 com as chaves
    todas presentes - uma celula zerada e informacao, nao ausencia."""
    matriz = _matriz_vazia()
    comparaveis = iguais = 0
    for a, b in pares:
        if a in NIVEIS and b in NIVEIS:
            comparaveis += 1
            iguais += a == b
            matriz[a][b] += 1
    return {
        "comparaveis": comparaveis,
        "concordancia": _fracao(iguais, comparaveis),
        "matriz": matriz,
    }


def _momento(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")


def tempo_em_analise(trilha: list[sqlite3.Row | dict]) -> float | None:
    """Segundos que o caso passou em analise ate a decisao, pela trilha.

    Soma TODOS os periodos em em_analise: um caso pego, devolvido e repego
    (por outro analista ou pelo mesmo) conta os dois periodos - foi trabalho
    de analise nos dois.

    None quando a trilha nao permite medir: sem a conclusao, ou com uma saida
    de em_analise sem a entrada correspondente (caso pego antes de a trilha
    existir, no esquema v5). Um tempo parcial apresentado como total seria pior
    que nenhum.
    """
    total = 0.0
    inicio = None
    concluiu = False
    for t in trilha:
        if t["estado_novo"] == "em_analise":
            inicio = _momento(t["registrado_em"])
        elif t["estado_anterior"] == "em_analise":
            if inicio is None:
                return None
            total += (_momento(t["registrado_em"]) - inicio).total_seconds()
            inicio = None
            concluiu = concluiu or t["estado_novo"] == "concluido"
    return total if concluiu else None


def calcular(conn: sqlite3.Connection, execucao_id: int | None,
             linha_de_base_min: float | None = None) -> dict:
    """`execucao_id=None`: TODAS as decisoes do store - inclusive as tomadas em
    alertas que depois foram substituidos por dado novo (a decisao foi real e
    conta) - e `casos` = alertas vigentes. Com execucao_id, so aquela rodada."""
    from mesa.repositorio import VIGENTE

    filtro, params = ("", ()) if execucao_id is None else ("WHERE a.execucao_id = ?", (execucao_id,))
    decisoes = conn.execute(
        f"""
        SELECT d.alerta_id, d.decisao, d.nivel_risco_analista, d.nivel_risco_agente,
               d.parecer_id, a.nivel_risco_regra, ad.fundamentado
        FROM decisoes d
        JOIN alertas a ON a.id = d.alerta_id
        LEFT JOIN aderencia ad ON ad.parecer_id = d.parecer_id
        {filtro}
        ORDER BY d.alerta_id
        """,
        params,
    ).fetchall()
    total_casos = conn.execute(
        f"SELECT COUNT(*) FROM alertas a WHERE {VIGENTE if execucao_id is None else 'a.execucao_id = ?'}",
        params,
    ).fetchone()[0]

    por_decisao = {d: 0 for d in DECISOES}
    for d in decisoes:
        por_decisao[d["decisao"]] += 1

    # --- agente x analista: a metrica-mor a partir desta fase ---
    agente_analista = _comparacao([(d["nivel_risco_agente"], d["nivel_risco_analista"]) for d in decisoes])
    com_parecer = [d for d in decisoes if d["nivel_risco_agente"] is not None]
    agente_analista["aceitacao_do_parecer"] = _fracao(
        sum(d["decisao"] == "concordo" for d in com_parecer), len(com_parecer)
    )
    agente_analista["decididos_com_parecer"] = len(com_parecer)

    # --- regra x analista e regra x agente, sobre os MESMOS casos decididos,
    # para a metrica antiga poder ser lida ao lado da nova ---
    regra_analista = _comparacao(
        [(_normalizar_nivel(d["nivel_risco_regra"]), d["nivel_risco_analista"]) for d in decisoes]
    )
    regra_agente = _comparacao(
        [(_normalizar_nivel(d["nivel_risco_regra"]), d["nivel_risco_agente"]) for d in decisoes]
    )

    # --- aderencia x decisao: o verificador acerta o que o analista rejeita? ---
    grupos = {"fundamentado": [], "nao_fundamentado": [], "sem_verificacao": []}
    for d in decisoes:
        chave = ("sem_verificacao" if d["fundamentado"] is None
                 else "fundamentado" if d["fundamentado"] else "nao_fundamentado")
        grupos[chave].append(d["decisao"])
    aderencia = {}
    for chave, lista in grupos.items():
        rejeitados = sum(x != "concordo" for x in lista)
        aderencia[chave] = {
            "decididos": len(lista),
            **{x: lista.count(x) for x in DECISOES},
            # "rejeitar" = nao concordar: discordar ou escalar
            "taxa_de_rejeicao": _fracao(rejeitados, len(lista)),
        }

    return {
        "execucao_id": execucao_id,
        "casos": total_casos,
        "decididos": len(decisoes),
        "por_decisao": por_decisao,
        "agente_vs_analista": agente_analista,
        "regra_vs_analista": regra_analista,
        "regra_vs_agente": regra_agente,
        "aderencia_vs_decisao": aderencia,
        "tempo": _tempo(conn, [d["alerta_id"] for d in decisoes],
                        {d["alerta_id"]: d["decisao"] for d in decisoes}, linha_de_base_min),
    }


def _tempo(conn: sqlite3.Connection, alertas: list[int], decisao_de: dict[int, str],
           linha_de_base_min: float | None) -> dict:
    medidos: dict[int, float] = {}
    for alerta_id in alertas:
        trilha = conn.execute(
            "SELECT estado_anterior, estado_novo, registrado_em FROM transicoes "
            "WHERE alerta_id = ? ORDER BY id",
            (alerta_id,),
        ).fetchall()
        segundos = tempo_em_analise(trilha)
        if segundos is not None:
            medidos[alerta_id] = segundos

    valores = list(medidos.values())
    mediana = statistics.median(valores) if valores else None
    por_decisao = {}
    for x in DECISOES:
        deste = [s for a, s in medidos.items() if decisao_de[a] == x]
        por_decisao[x] = {
            "casos": len(deste),
            "mediana_s": statistics.median(deste) if deste else None,
        }

    linha_de_base = None
    economia = None
    if linha_de_base_min is not None:
        linha_de_base = {"minutos": linha_de_base_min, "procedencia": PROCEDENCIA_LINHA_DE_BASE}
        if mediana is not None:
            economia = linha_de_base_min * 60 - mediana

    return {
        "casos_medidos": len(valores),
        # decididos cuja trilha nao permite medir (ex.: pegos antes do esquema
        # v6) - declarados, nao descartados em silencio
        "casos_sem_trilha_completa": len(alertas) - len(valores),
        "mediana_s": mediana,
        "media_s": statistics.fmean(valores) if valores else None,
        "por_decisao": por_decisao,
        "linha_de_base": linha_de_base,
        # linha de base - mediana medida, por caso. Negativo e possivel e fica
        # negativo: a ferramenta pode estar custando tempo, e isso tambem e dado
        "economia_mediana_s": economia,
    }
