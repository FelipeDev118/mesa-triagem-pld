"""Fase 5.3 - o ciclo agendavel.

O que um agendador precisa do ciclo: rodar de novo sem arquivo novo nao faz
NADA (nem lote, nem execucao, nem chamada de API); arquivo novo e processado
uma vez; dois ciclos ao mesmo tempo nao se atropelam.
"""
import json
import shutil

import pytest
from tests_apoio import resposta_final

import agente
from mesa import ciclo, db, regras_run, triagem
from mesa.importar_cache import importar
from mesa.ingestao import ingerir

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"


@pytest.fixture
def banco(tmp_path):
    """Store no estado de uma instalacao em uso: base da entrega ingerida,
    regras rodadas, historico importado, 30 alertas triados."""
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    ingerir(conn, DADOS_REAIS)
    regras_run.executar(conn)
    importar(conn)
    conn.close()
    return caminho


@pytest.fixture
def api_llm(monkeypatch):
    chamadas = {"n": 0}

    def fake(**kwargs):
        chamadas["n"] += 1
        return resposta_final()

    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", fake)
    return chamadas


def _contagens(caminho):
    conn = db.conectar(caminho)
    r = tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("lotes_ingestao", "execucoes_regras", "alertas", "pareceres"))
    conn.close()
    return r


def _arquivo_novo(pasta, nome="2026-10-01.json"):
    ops = json.loads(DADOS_REAIS.read_text(encoding="utf-8"))["operacoes"]
    nova = {**next(o for o in ops if o["cliente_id"] == "CLI-011"), "id": "OP-90001", "valor": 321.0}
    (pasta / nome).write_text(json.dumps({"taxa_cambio_usd_brl": 5.4, "operacoes": [nova]}),
                              encoding="utf-8")


def test_ciclo_sem_arquivo_novo_nao_faz_nada(banco, tmp_path, api_llm):
    """<<< aceite do 5.3 >>> A pasta tem a MESMA base ja ingerida (outro nome):
    o sha256 a reconhece. A primeira passada so termina a triagem pendente
    (sem API: o historico importado cobre); as seguintes nao gravam nada."""
    entrada = tmp_path / "entrada"
    entrada.mkdir()
    shutil.copy(DADOS_REAIS, entrada / "base-renomeada.json")

    primeira = ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
    assert (primeira.arquivos_ingeridos, primeira.arquivos_ja_conhecidos) == ([], 1)
    assert (primeira.execucao, primeira.triados, api_llm["n"]) == (None, 30, 0)

    retrato = _contagens(banco)
    for _ in range(2):
        r = ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
        assert (r.arquivos_ingeridos, r.execucao, r.triados, r.chamadas_api) == ([], None, 0, 0)
    assert _contagens(banco) == retrato
    assert api_llm["n"] == 0


def test_arquivo_novo_e_processado_uma_vez(banco, tmp_path, api_llm):
    """<<< aceite do 5.3 >>> Um arquivo com uma operacao nova do CLI-011: um
    lote, uma execucao incremental, um alerta triado, UMA chamada de API. Na
    passada seguinte, nada."""
    entrada = tmp_path / "entrada"
    entrada.mkdir()
    ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)  # termina a triagem inicial
    _arquivo_novo(entrada)

    r = ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
    assert r.arquivos_ingeridos == ["2026-10-01.json"]
    assert r.execucao.endswith("(incremental)")
    assert (r.triados, r.chamadas_api, api_llm["n"]) == (1, 1, 1)

    # de novo, e com o mesmo arquivo copiado com outro nome: nada
    shutil.copy(entrada / "2026-10-01.json", entrada / "copia.json")
    r = ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
    assert (r.arquivos_ingeridos, r.arquivos_ja_conhecidos, r.execucao, r.triados) == ([], 2, None, 0)
    assert api_llm["n"] == 1


def test_segundo_ciclo_simultaneo_sai_sem_fazer_nada(banco, tmp_path, api_llm):
    """<<< aceite do 5.3 >>> Com a trava tomada (um ciclo em andamento), outro
    ciclo nao ingere nem tria - mesmo havendo arquivo novo na pasta."""
    entrada = tmp_path / "entrada"
    entrada.mkdir()
    _arquivo_novo(entrada)
    antes = _contagens(banco)

    with ciclo._trava(banco):
        r = ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
    assert r.executado is False
    assert _contagens(banco) == antes

    # trava liberada: agora processa
    assert ciclo.rodar(entrada, banco, pausa_s=0, verbose=False).arquivos_ingeridos == ["2026-10-01.json"]


def test_ciclo_retoma_triagem_de_um_ciclo_que_caiu(banco, tmp_path, api_llm, monkeypatch):
    """Um ciclo que cai entre as regras e a triagem deixa alerta 'novo'. O
    proximo, mesmo sem arquivo novo e sem execucao nova, tria o que ficou."""
    entrada = tmp_path / "entrada"
    entrada.mkdir()
    ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
    _arquivo_novo(entrada)

    def triagem_que_cai(*a, **k):
        raise KeyboardInterrupt("container reiniciado")

    monkeypatch.setattr(triagem, "triar", triagem_que_cai)
    with pytest.raises(KeyboardInterrupt):
        ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
    monkeypatch.undo()
    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", lambda **k: resposta_final())

    r = ciclo.rodar(entrada, banco, pausa_s=0, verbose=False)
    assert (r.arquivos_ingeridos, r.execucao, r.triados) == ([], None, 1)


def test_main_roda_uma_passada(banco, tmp_path, monkeypatch, capsys, api_llm):
    monkeypatch.setattr(db, "CAMINHO_PADRAO", banco)
    assert ciclo.main(["--entrada", str(tmp_path / "entrada")]) == 0
    assert "ciclo:" in capsys.readouterr().out
