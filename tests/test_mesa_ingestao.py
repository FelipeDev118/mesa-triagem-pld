"""Passos 0.2 e 0.3 - ingestao e idempotencia.

Os numeros conferidos aqui sao os da entrega (docs/REPRODUTIBILIDADE.md): se a
ingestao mudar qualquer um deles, ela esta errada - nao a entrega.
"""
import json

import pytest
from conftest import escrever_dataset, op

from mesa import db
from mesa.ingestao import ingerir

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"


@pytest.fixture
def conn(tmp_path):
    c = db.conectar(tmp_path / "mesa.db")
    yield c
    c.close()


# ---------- 0.2: os numeros da entrega ----------


def test_ingestao_da_base_real_reproduz_os_numeros_da_entrega(conn):
    r = ingerir(conn, DADOS_REAIS)

    assert r.operacoes_brutas == 322
    assert r.operacoes_inseridas == 317
    assert r.duplicatas_ignoradas == 5
    assert conn.execute("SELECT COUNT(*) FROM operacoes").fetchone()[0] == 317
    assert conn.execute("SELECT COUNT(DISTINCT cliente_id) FROM operacoes").fetchone()[0] == 30


def test_estatisticas_do_arquivo_bruto_ficam_no_lote(conn):
    """As 7 datas nulas do ARQUIVO viram 6 na tabela porque uma delas esta numa
    das 5 duplicatas removidas. Os dois numeros estao certos, sobre populacoes
    diferentes - e ambos precisam ser recuperaveis do store."""
    ingerir(conn, DADOS_REAIS)

    lote = conn.execute("SELECT * FROM lotes_ingestao").fetchone()
    assert lote["datas_nulas_brutas"] == 7
    assert lote["operacoes_usd_brutas"] == 7
    assert lote["taxa_cambio_usd_brl"] == 5.4

    na_tabela = conn.execute(
        "SELECT COUNT(*) FROM operacoes WHERE data IS NULL"
    ).fetchone()[0]
    assert na_tabela == 6


def test_data_nula_grava_null_e_data_valida_zero(conn):
    ingerir(conn, DADOS_REAIS)
    incoerentes = conn.execute(
        "SELECT COUNT(*) FROM operacoes "
        "WHERE (data IS NULL) != (data_valida = 0)"
    ).fetchone()[0]
    assert incoerentes == 0


def test_conversao_usd_usa_a_taxa_do_lote(conn):
    ingerir(conn, DADOS_REAIS)
    fora = conn.execute(
        "SELECT COUNT(*) FROM operacoes "
        "WHERE moeda = 'USD' AND abs(valor_brl - valor * 5.4) > 1e-9"
    ).fetchone()[0]
    assert fora == 0

    intactas = conn.execute(
        "SELECT COUNT(*) FROM operacoes WHERE moeda = 'BRL' AND valor_brl != valor"
    ).fetchone()[0]
    assert intactas == 0


def test_taxa_e_do_lote_nao_uma_constante_global(conn, tmp_path):
    """Dois arquivos com taxas diferentes: cada operacao guarda a taxa que valia
    na SUA ingestao. E o que impede um alerta antigo de mudar de valor quando o
    cambio muda."""
    a = escrever_dataset(tmp_path, [op("OP-A", "CLI-1", "2026-01-01", 100.0, moeda="USD")], taxa=5.0)
    b = tmp_path / "b.json"
    b.write_text(
        json.dumps({"taxa_cambio_usd_brl": 6.0, "operacoes": [
            op("OP-B", "CLI-1", "2026-01-02", 100.0, moeda="USD")]}),
        encoding="utf-8",
    )

    ingerir(conn, a)
    ingerir(conn, b)

    valores = dict(conn.execute("SELECT id, valor_brl FROM operacoes").fetchall())
    assert valores["OP-A"] == pytest.approx(500.0)
    assert valores["OP-B"] == pytest.approx(600.0)


# ---------- 0.3: idempotencia ----------


def test_reingerir_o_mesmo_arquivo_nao_duplica(conn):
    primeira = ingerir(conn, DADOS_REAIS)
    segunda = ingerir(conn, DADOS_REAIS)

    assert primeira.operacoes_inseridas == 317
    assert segunda.operacoes_inseridas == 0
    assert segunda.duplicatas_ignoradas == 322
    assert conn.execute("SELECT COUNT(*) FROM operacoes").fetchone()[0] == 317
    # o lote da 2a tentativa fica registrado: "alguem tentou reingerir" e um fato
    assert conn.execute("SELECT COUNT(*) FROM lotes_ingestao").fetchone()[0] == 2


def test_operacao_continua_apontando_para_o_lote_que_a_trouxe(conn):
    primeira = ingerir(conn, DADOS_REAIS)
    ingerir(conn, DADOS_REAIS)
    do_segundo = conn.execute(
        "SELECT COUNT(*) FROM operacoes WHERE lote_id != ?", (primeira.lote_id,)
    ).fetchone()[0]
    assert do_segundo == 0


# ---------- 5.2: operacao corrigida vira versao nova, a anterior fica ----------


def _arquivo(tmp_path, nome, operacoes, taxa=5.4):
    caminho = tmp_path / nome
    caminho.write_text(json.dumps({"taxa_cambio_usd_brl": taxa, "operacoes": operacoes}),
                       encoding="utf-8")
    return caminho


def test_arquivo_corrigido_grava_versao_nova_e_guarda_a_anterior(conn, tmp_path):
    """<<< aceite do 5.2 >>> Ate a Fase 4 isto era divida fixada em teste: o
    mesmo id com valor diferente era IGNORADO. Agora vira versao nova, com o
    lote que a trouxe, e a anterior fica no historico - fato nao muda em
    silencio, mas tambem nao fica errado para sempre."""
    ingerir(conn, _arquivo(tmp_path, "a.json", [op("OP-1", "CLI-1", "2026-01-01", 100.0),
                                                 op("OP-2", "CLI-2", "2026-01-01", 50.0)]))
    r = ingerir(conn, _arquivo(tmp_path, "b.json", [op("OP-1", "CLI-1", "2026-01-01", 999.0),
                                                     op("OP-2", "CLI-2", "2026-01-01", 50.0)]))

    assert (r.operacoes_inseridas, r.operacoes_corrigidas, r.duplicatas_ignoradas) == (0, 1, 1)
    assert r.clientes_afetados == {"CLI-1"}
    atual = conn.execute("SELECT valor, lote_id FROM operacoes WHERE id='OP-1'").fetchone()
    assert tuple(atual) == (999.0, r.lote_id)
    hist = conn.execute("SELECT operacao_id, valor, lote_id, substituida_pelo_lote "
                        "FROM operacoes_historico").fetchall()
    assert [tuple(h) for h in hist] == [("OP-1", 100.0, 1, r.lote_id)]


def test_mesmo_arquivo_com_outra_taxa_nao_e_correcao(conn, tmp_path):
    """valor_brl e derivado com a taxa do LOTE. Reenviar a mesma operacao em USD
    num lote com outra taxa nao corrige nada - e o valor_brl gravado continua o
    da taxa que valia quando ela entrou."""
    ingerir(conn, _arquivo(tmp_path, "a.json", [op("OP-1", "CLI-1", "2026-01-01", 100.0, "USD")], 5.4))
    r = ingerir(conn, _arquivo(tmp_path, "b.json", [op("OP-1", "CLI-1", "2026-01-01", 100.0, "USD")], 6.0))

    assert (r.operacoes_corrigidas, r.duplicatas_ignoradas) == (0, 1)
    assert r.clientes_afetados == frozenset()
    assert conn.execute("SELECT valor_brl FROM operacoes").fetchone()[0] == pytest.approx(540.0)
    assert conn.execute("SELECT COUNT(*) FROM operacoes_historico").fetchone()[0] == 0


def test_correcao_em_usd_recalcula_com_a_taxa_do_lote_novo(conn, tmp_path):
    ingerir(conn, _arquivo(tmp_path, "a.json", [op("OP-1", "CLI-1", "2026-01-01", 100.0, "USD")], 5.4))
    ingerir(conn, _arquivo(tmp_path, "b.json", [op("OP-1", "CLI-1", "2026-01-01", 200.0, "USD")], 6.0))
    assert conn.execute("SELECT valor_brl FROM operacoes").fetchone()[0] == pytest.approx(1200.0)
    assert conn.execute("SELECT valor_brl FROM operacoes_historico").fetchone()[0] == pytest.approx(540.0)


def test_correcao_que_troca_o_cliente_afeta_os_dois(conn, tmp_path):
    """Os dois clientes tem o resultado das regras alterado: um perde a
    operacao, o outro ganha. Os dois entram no delta."""
    ingerir(conn, _arquivo(tmp_path, "a.json", [op("OP-1", "CLI-1", "2026-01-01", 100.0)]))
    r = ingerir(conn, _arquivo(tmp_path, "b.json", [op("OP-1", "CLI-9", "2026-01-01", 100.0)]))
    assert r.clientes_afetados == {"CLI-1", "CLI-9"}


def test_arquivo_novo_e_incremental_ausencia_nao_apaga(conn, tmp_path):
    ingerir(conn, _arquivo(tmp_path, "a.json", [op("OP-1", "CLI-1", "2026-01-01", 100.0)]))
    r = ingerir(conn, _arquivo(tmp_path, "b.json", [op("OP-2", "CLI-2", "2026-01-02", 70.0)]))
    assert r.clientes_afetados == {"CLI-2"}
    assert conn.execute("SELECT COUNT(*) FROM operacoes").fetchone()[0] == 2


def test_operacao_nao_se_apaga_nem_se_corrige_sem_lote_novo(conn, tmp_path):
    """As duas garantias moram no schema, nao na ingestao: vale para qualquer SQL."""
    import sqlite3

    ingerir(conn, _arquivo(tmp_path, "a.json", [op("OP-1", "CLI-1", "2026-01-01", 100.0)]))
    with pytest.raises(sqlite3.IntegrityError, match="nao se apaga"):
        conn.execute("DELETE FROM operacoes")
    with pytest.raises(sqlite3.IntegrityError, match="lote posterior"):
        conn.execute("UPDATE operacoes SET valor = 1")  # mesmo lote_id
    conn.rollback()
    # trigger de linha nao dispara em tabela vazia: precisa haver uma versao
    ingerir(conn, _arquivo(tmp_path, "b.json", [op("OP-1", "CLI-1", "2026-01-01", 200.0)]))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM operacoes_historico")
