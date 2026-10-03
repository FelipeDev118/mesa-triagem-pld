"""Passo 0.4 - leitura do store no formato que as regras ja esperam.

A fronteira entre store e regras e um DataFrame com colunas conhecidas. Por
isso `flag_fracionamento()`, `flag_valor_atipico()` e `ranking_clientes_
sinalizados()` nao mudam uma linha ao trocarmos JSON por SQLite: elas nunca
souberam de onde o DataFrame veio, e continuam sem saber.

O contrato que este modulo tem que honrar, e que o teste 0.4 verifica, e forte:
o DataFrame lido do store precisa ser IGUAL ao devolvido por
`carregar_e_limpar()` - mesmas colunas, mesma ordem de linhas, mesmos dtypes.
Igual, nao "equivalente": diferenca de ordem muda desempate de `value_counts()`
em `historico_cliente()`, que entra no prompt do agente, que entra no hash do
parecer. Um detalhe de ordenacao vira invalidacao de cache tres camadas adiante.
"""
import sqlite3

import pandas as pd

# Alerta VIGENTE = o que nenhum outro substitui (Fase 5.1). Uma definicao so,
# usada pela fila, pelo worker, pelas metricas, pelo exportar, pelo confronto e
# pelo verificar_ambiente - todos antes liam "a execucao mais recente", que com
# execucao incremental passou a ser so o delta. Espera o alias `a` para alertas.
VIGENTE = "NOT EXISTS (SELECT 1 FROM alertas s WHERE s.substitui_alerta_id = a.id)"

# Ordem identica a do DataFrame produzido por dados.carregar_e_limpar(): as 9
# colunas do JSON na ordem em que aparecem no arquivo, depois as 2 derivadas.
COLUNAS = [
    "id", "cliente_id", "data", "valor", "moeda", "canal", "tipo",
    "contraparte", "observacao", "data_valida", "valor_brl",
]


def operacoes_df(conn: sqlite3.Connection, cliente_id: str | None = None) -> pd.DataFrame:
    """Devolve as operacoes do store no formato da camada de regras.

    `ORDER BY rowid` = ordem de insercao = ordem do arquivo apos a deduplicacao.
    Nao e detalhe estetico (ver docstring do modulo).

    `cliente_id` usa o indice idx_op_cliente em vez de trazer a base inteira e
    filtrar em pandas - e a razao de o store existir.
    """
    sql = f"SELECT {', '.join(COLUNAS)} FROM operacoes"
    params: tuple = ()
    if cliente_id is not None:
        sql += " WHERE cliente_id = ?"
        params = (cliente_id,)
    sql += " ORDER BY rowid"

    df = pd.read_sql_query(sql, conn, params=params)

    # SQLite nao tem tipo data nem booleano; a conversao de volta e aqui, num
    # lugar so, e nao espalhada por quem consome.
    df["data"] = pd.to_datetime(df["data"], format="%Y-%m-%d", errors="coerce")
    df["data_valida"] = df["data_valida"].astype(bool)
    return df


def clientes(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT cliente_id FROM operacoes ORDER BY cliente_id"
        )
    ]


def taxa_do_ultimo_lote(conn: sqlite3.Connection) -> float:
    """A taxa que `carregar_e_limpar()` devolvia como segundo elemento.

    Existe por compatibilidade com quem espera aquele par, mas note a mudanca de
    significado: no JSON havia UMA taxa; no store cada operacao ja carrega o
    valor_brl calculado com a taxa do proprio lote. Esta funcao e a taxa da
    ingestao mais recente, nao "a taxa do sistema".
    """
    linha = conn.execute(
        "SELECT taxa_cambio_usd_brl FROM lotes_ingestao ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if linha is None:
        raise RuntimeError("store vazio: nenhuma ingestao registrada")
    return float(linha[0])


def operacoes_no_lote(conn: sqlite3.Connection, lote_id: int,
                      cliente_id: str | None = None) -> list[sqlite3.Row]:
    """As operacoes COMO ERAM depois do lote `lote_id` (Fase 5.2).

    Uma operacao entra se ja existia naquele lote, na versao vigente nele: a
    atual, se chegou ate o lote; ou a do historico que ainda nao tinha sido
    substituida. Operacao que chegou depois nao aparece. E o que deixa o caso
    decidido ontem ser exibido com a base de ontem, e nao com a de hoje.

    Ordem da tela: sem data no fim, depois data, depois id.
    """
    filtro, params = "", {"lote": lote_id}
    if cliente_id is not None:
        filtro, params["cliente"] = " AND cliente_id = :cliente", cliente_id
    colunas = "cliente_id, data, valor, moeda, valor_brl, canal, tipo, contraparte, data_valida"
    return conn.execute(
        # subconsulta porque o ORDER BY de um UNION so aceita coluna, nao
        # expressao como `data IS NULL`
        f"""
        SELECT * FROM (
            SELECT id, {colunas}, lote_id FROM operacoes
            WHERE lote_id <= :lote{filtro}
            UNION ALL
            SELECT operacao_id AS id, {colunas}, lote_id FROM operacoes_historico
            WHERE lote_id <= :lote AND substituida_pelo_lote > :lote{filtro}
        )
        ORDER BY data IS NULL, data, id
        """,
        params,
    ).fetchall()
