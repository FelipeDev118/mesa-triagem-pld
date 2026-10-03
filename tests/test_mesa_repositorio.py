"""Passo 0.4 - o DataFrame lido do store e igual ao lido do JSON.

Este e o teste que sustenta a Fase 0 inteira: se ele passa, trocar a fonte de
dados nao pode alterar nenhuma regra, porque as regras recebem exatamente o
mesmo objeto.
"""
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from dados import aplicar_regras, carregar_e_limpar, ranking_clientes_sinalizados
from mesa import db, repositorio
from mesa.ingestao import ingerir

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"


@pytest.fixture
def conn(tmp_path):
    c = db.conectar(tmp_path / "mesa.db")
    ingerir(c, DADOS_REAIS)
    yield c
    c.close()


def test_dataframe_do_store_e_igual_ao_do_json(conn):
    do_store = repositorio.operacoes_df(conn)
    do_json, _ = carregar_e_limpar(DADOS_REAIS)

    # A unica diferenca aceita e o INDICE: carregar_e_limpar() faz
    # drop_duplicates() sem reset_index, entao o indice do JSON tem buracos nas
    # 5 posicoes removidas (0..321 menos 5), enquanto o store nao tem como saber
    # a posicao original da linha no arquivo. Nao afeta nada a jusante: as regras
    # agregam por cliente/data e o unico merge de aplicar_regras() e por "id",
    # nunca por rotulo de indice.
    assert_frame_equal(do_store, do_json.reset_index(drop=True))


def test_ordem_das_linhas_e_preservada(conn):
    """Ordem importa: value_counts() em historico_cliente() desempata pela ordem
    de aparicao, o resultado entra no prompt, o prompt entra no hash do parecer."""
    do_store = repositorio.operacoes_df(conn)
    do_json, _ = carregar_e_limpar(DADOS_REAIS)
    assert list(do_store["id"]) == list(do_json["id"])


def test_regras_dao_o_mesmo_resultado_pelas_duas_vias(conn):
    """O que de fato importa: as regras nao distinguem a origem."""
    regras_store = aplicar_regras(repositorio.operacoes_df(conn))
    regras_json = aplicar_regras(carregar_e_limpar(DADOS_REAIS)[0])

    assert_frame_equal(regras_store, regras_json.reset_index(drop=True))
    assert_frame_equal(
        ranking_clientes_sinalizados(regras_store),
        ranking_clientes_sinalizados(regras_json),
    )


def test_filtro_por_cliente_devolve_o_mesmo_recorte(conn):
    do_store = repositorio.operacoes_df(conn, cliente_id="CLI-014")
    do_json, _ = carregar_e_limpar(DADOS_REAIS)
    esperado = do_json[do_json["cliente_id"] == "CLI-014"].reset_index(drop=True)

    assert_frame_equal(do_store, esperado)
    assert len(do_store) == 11


def test_filtro_por_cliente_usa_o_indice(conn):
    """O indice existir no schema nao garante que a query o use - o planner e
    quem decide. EXPLAIN QUERY PLAN e a unica forma de afirmar isso."""
    plano = conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM operacoes WHERE cliente_id = 'CLI-014'"
    ).fetchall()
    texto = " ".join(r["detail"] for r in plano)
    assert "idx_op_cliente" in texto, texto
    assert "SCAN" not in texto, texto


def test_data_volta_como_datetime_com_nat(conn):
    df = repositorio.operacoes_df(conn)
    assert pd.api.types.is_datetime64_any_dtype(df["data"])
    assert df["data"].isna().sum() == 6
    assert (~df["data_valida"]).sum() == 6


def test_taxa_do_ultimo_lote(conn):
    assert repositorio.taxa_do_ultimo_lote(conn) == 5.4


def test_store_vazio_falha_claro(tmp_path):
    c = db.conectar(tmp_path / "vazio.db")
    with pytest.raises(RuntimeError, match="store vazio"):
        repositorio.taxa_do_ultimo_lote(c)
    assert repositorio.operacoes_df(c).empty
    c.close()
