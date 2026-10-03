"""Passo 0.8 - o portao da Fase 0.

Nao basta o comando imprimir OK uma vez na minha maquina: o teste garante que
os dois modos concordam e que o modo --store FALHA quando deve, em vez de
aprovar um store incompleto ou executado com outros parametros.
"""
import pytest

import verificar_ambiente as va
from dados import PARAMETROS_REGRAS
from mesa import db, regras_run
from mesa.ingestao import ingerir

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Store completo num caminho temporario, no lugar do outputs/mesa.db real."""
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    ingerir(conn, DADOS_REAIS)
    regras_run.executar(conn)
    conn.close()
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)
    return caminho


def test_store_reproduz_exatamente_o_esperado_da_entrega(store):
    obtido, taxa = va.obter_do_store()
    assert obtido == va.ESPERADO
    assert taxa == 5.4


def test_os_dois_modos_dao_o_mesmo_resultado(store):
    do_store, taxa_store = va.obter_do_store()
    do_json, taxa_json = va.obter_do_json()
    assert do_store == do_json
    assert taxa_store == taxa_json


def test_main_retorna_zero_no_modo_store(store, capsys):
    assert va.main(usar_store=True) == 0
    assert "OK: a camada deterministica reproduz" in capsys.readouterr().out


def test_store_inexistente_orienta_em_vez_de_estourar(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "CAMINHO_PADRAO", tmp_path / "nao-existe.db")
    with pytest.raises(SystemExit, match="mesa.ingestao"):
        va.obter_do_store()


def test_store_sem_execucao_de_regras_e_recusado(tmp_path, monkeypatch):
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    ingerir(conn, DADOS_REAIS)
    conn.close()
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)

    with pytest.raises(SystemExit, match="mesa.regras_run"):
        va.obter_do_store()


def test_execucao_com_outros_parametros_e_recusada_em_vez_de_reprovada(tmp_path, monkeypatch):
    """Distincao que importa: comparar contra o ESPERADO da entrega uma execucao
    feita com outro limiar reprovaria pelo motivo errado. O script recusa a
    comparacao e diz por que, em vez de apontar 'FALHOU' num numero legitimo."""
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    ingerir(conn, DADOS_REAIS)
    regras_run.executar(conn)
    regras_run.executar(conn, {**PARAMETROS_REGRAS, "atipico_fator": 10})
    conn.close()
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)

    with pytest.raises(SystemExit, match="parametros diferentes"):
        va.obter_do_store()


def test_reingestao_nao_infla_os_numeros_brutos(tmp_path, monkeypatch):
    """Um 2o lote com 0 operacoes inseridas nao pode virar 644 operacoes brutas."""
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    ingerir(conn, DADOS_REAIS)
    ingerir(conn, DADOS_REAIS)
    regras_run.executar(conn)
    conn.close()
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)

    obtido, _ = va.obter_do_store()
    assert obtido == va.ESPERADO
