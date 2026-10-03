"""Carregamento, limpeza e regras determinísticas — mesma lógica do Nível 1,
reaproveitada aqui sobre a base maior (~320 operações, 30 clientes).

Ver docs/DECISOES.md para a justificativa de cada tratamento (duplicatas, datas
nulas, conversão de moeda) — é a mesma do Nível 1, só que agora encapsulada em
funções para reuso pelas ferramentas, pelo agente e pelo script de confronto.
"""
import json
from pathlib import Path

import pandas as pd

DADOS_PATH = Path(__file__).resolve().parent.parent / "dados" / "dados_nivel_2.json"

# Os limiares das duas regras, num lugar so. Estavam embutidos como numeros
# magicos dentro de flag_fracionamento() e flag_valor_atipico().
#
# O motivo de extrair nao e estetica nem configurabilidade: e AUDITORIA. A Mesa
# de Triagem grava estes valores junto de cada execucao de regras
# (execucoes_regras.parametros_json), para que um alerta continue explicavel
# depois que o limiar mudar. "Por que R$ 50.000?" nao pode depender de alguem
# fazer arqueologia no git para descobrir o que valia naquele dia.
#
# Os valores sao os do enunciado e NAO mudaram nesta extracao - os testes de
# fronteira em tests/test_dados.py sao a prova disso.
PARAMETROS_REGRAS = {
    "frac_min_ops": 3,             # Regra 1: operacoes no mesmo dia
    "frac_soma_min": 50000,        # Regra 1: soma do dia acima de
    "frac_max_individual": 20000,  # Regra 1: nenhuma operacao atinge
    "atipico_min_ops": 4,          # Regra 2: guarda de historico minimo
    "atipico_fator": 5,            # Regra 2: multiplo da mediana do cliente
}

# Muda quando a LOGICA das regras mudar (nao quando so um limiar mudar - isso ja
# fica registrado em parametros_json). Ex.: adicionar janela deslizante na
# Regra 1 seria r2.
VERSAO_REGRAS = "r1-2026-09-12"


def carregar_e_limpar(path: Path = DADOS_PATH) -> tuple[pd.DataFrame, float]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    taxa = raw["taxa_cambio_usd_brl"]
    df = pd.DataFrame(raw["operacoes"])

    df = df.drop_duplicates(subset=["id"]).copy()
    # Formato FIXO (ISO, o da base). Sem ele o pandas adivinha pelo primeiro
    # elemento via dateutil: num arquivo so com datas brasileiras, "05/03/2026"
    # virava 3 de MAIO e "13/03/2026" virava invalida - medido. Data fora do
    # formato vira invalida (data_valida=False, visivel e tratada pelas
    # regras), nunca uma data errada em silencio. Achado na auditoria da Fase 5,
    # quando arquivos novos passaram a chegar pelo ciclo.
    df["data"] = pd.to_datetime(df["data"], format="%Y-%m-%d", errors="coerce")
    df["data_valida"] = df["data"].notna()
    df["valor_brl"] = df.apply(
        lambda r: r["valor"] * taxa if r["moeda"] == "USD" else r["valor"],
        axis=1,
    )
    return df, taxa


def flag_fracionamento(df: pd.DataFrame, parametros: dict = PARAMETROS_REGRAS) -> pd.DataFrame:
    elegivel = df[df["data_valida"]]
    grp = elegivel.groupby(["cliente_id", "data"])["valor_brl"]
    candidatos = pd.DataFrame(
        {"soma": grp.sum(), "qtd": grp.count(), "max_individual": grp.max()}
    ).reset_index()
    return candidatos[
        (candidatos["qtd"] >= parametros["frac_min_ops"])
        & (candidatos["soma"] > parametros["frac_soma_min"])
        & (candidatos["max_individual"] < parametros["frac_max_individual"])
    ]


def datas_fracionamento(df: pd.DataFrame, cliente_id: str,
                        parametros: dict = PARAMETROS_REGRAS) -> list[str]:
    """Quais datas dispararam a Regra 1 para este cliente, em YYYY-MM-DD.

    Existe porque aplicar_regras() colapsa o resultado de flag_fracionamento() (que tem
    cliente_id + data + soma + qtd) num booleano por cliente - suficiente para a coluna
    flag_fracionamento do DataFrame, mas insuficiente para o agente investigar o dia
    certo. Reusa flag_fracionamento() em vez de duplicar o calculo do candidato."""
    candidatos = flag_fracionamento(df, parametros)
    datas = candidatos.loc[candidatos["cliente_id"] == cliente_id, "data"]
    return sorted(d.strftime("%Y-%m-%d") for d in datas)


def flag_valor_atipico(df: pd.DataFrame, parametros: dict = PARAMETROS_REGRAS) -> pd.DataFrame:
    contagem = df.groupby("cliente_id")["id"].transform("count")
    elegivel = df[contagem >= parametros["atipico_min_ops"]].copy()
    mediana = elegivel.groupby("cliente_id")["valor_brl"].transform("median")
    elegivel["limite_atipico"] = mediana * parametros["atipico_fator"]
    elegivel["atipico"] = elegivel["valor_brl"] > elegivel["limite_atipico"]
    return elegivel[["id", "cliente_id", "valor_brl", "limite_atipico", "atipico"]]


def aplicar_regras(df: pd.DataFrame, parametros: dict = PARAMETROS_REGRAS) -> pd.DataFrame:
    df = df.copy()
    fracionamento = flag_fracionamento(df, parametros)
    clientes_fracionamento = set(fracionamento["cliente_id"])
    df["flag_fracionamento"] = df["cliente_id"].isin(clientes_fracionamento)

    atipicos = flag_valor_atipico(df, parametros)
    df = df.merge(
        atipicos[["id", "atipico"]].rename(columns={"atipico": "flag_valor_atipico"}),
        on="id",
        how="left",
    )
    df["flag_valor_atipico"] = df["flag_valor_atipico"].fillna(False)
    return df


def montar_flags(df: pd.DataFrame, row: pd.Series) -> dict:
    """Monta o dict de flags deterministicas que vai para o prompt do agente, a partir
    de uma linha de ranking_clientes_sinalizados().

    Existe como funcao unica porque essa montagem estava duplicada em tres lugares
    (nivel_2/lote.py, o teste standalone de nivel_2/agente.py e nivel_3/agente_mcp.py) -
    e foi exatamente por causa dessa duplicacao, uma vez, que um lugar recebeu a data do
    fracionamento e os outros nao. Um unico ponto de montagem evita reintroduzir isso."""
    cliente_id = row["cliente_id"]
    flags = {
        "flag_fracionamento": bool(row["sinalizacoes_fracionamento"]),
        "flag_valor_atipico": bool(row["sinalizacoes_valor_atipico"] > 0),
    }
    if flags["flag_fracionamento"]:
        flags["datas_fracionamento"] = datas_fracionamento(df, cliente_id)
    return flags


def _agregados_por_cliente(df: pd.DataFrame) -> pd.DataFrame:
    """Volume, contagem e sinalizações por cliente - TODOS os clientes da base, sem
    filtro nem ordenação. Extraída para ser reusada tanto por
    ranking_clientes_sinalizados() (só os sinalizados) quanto por todos_os_clientes()
    (a base inteira), sem duplicar o cálculo."""
    por_cliente = df.groupby("cliente_id").agg(
        volume_total_brl=("valor_brl", "sum"),
        qtd_operacoes=("id", "count"),
        sinalizacoes_fracionamento=("flag_fracionamento", "max"),
        sinalizacoes_valor_atipico=("flag_valor_atipico", "sum"),
    )
    por_cliente["sinalizacoes_fracionamento"] = por_cliente[
        "sinalizacoes_fracionamento"
    ].astype(int)
    por_cliente["total_sinalizacoes"] = (
        por_cliente["sinalizacoes_fracionamento"]
        + por_cliente["sinalizacoes_valor_atipico"]
    )
    return por_cliente


def ranking_clientes_sinalizados(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """Nº de sinalizações por cliente (operação com flag_valor_atipico=True conta 1,
    cliente com flag_fracionamento=True conta 1 sinalização de cliente), desempate por
    volume total. Ver DECISOES.md para o critério de contagem."""
    por_cliente = _agregados_por_cliente(df)
    ranking = por_cliente[por_cliente["total_sinalizacoes"] > 0].sort_values(
        ["total_sinalizacoes", "volume_total_brl"], ascending=[False, False]
    )
    return ranking.head(top_n).reset_index()


def todos_os_clientes(df: pd.DataFrame) -> pd.DataFrame:
    """Mesmas colunas de ranking_clientes_sinalizados(), mas para TODOS os clientes da
    base - inclusive os que nenhuma regra sinalizou (sinalizacoes_fracionamento=0,
    sinalizacoes_valor_atipico=0). Existe para medir o falso negativo que nenhuma
    métrica atual mede: um cliente sem flag determinística ainda poderia ser marcado
    como risco pelo agente? Ver DECISOES.md, "Cobrir os 30 clientes"."""
    por_cliente = _agregados_por_cliente(df)
    return por_cliente.sort_values(
        ["total_sinalizacoes", "volume_total_brl"], ascending=[False, False]
    ).reset_index()


if __name__ == "__main__":
    df, taxa = carregar_e_limpar()
    df = aplicar_regras(df)
    top10 = ranking_clientes_sinalizados(df)
    print(top10)
