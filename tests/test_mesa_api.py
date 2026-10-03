"""Fase 2 - contrato da API, um bloco por passo do ROADMAP.

Os stores de teste sao montados UMA vez por sessao (ingestao + regras + cache
importado + triagem) e copiados por arquivo para cada teste que escreve. Montar
do zero em cada teste custaria ~2s x dezenas de testes sem testar nada a mais.

O bloco de concorrencia no fim sobe um uvicorn de verdade. Nao e excesso: o
TestClient sequencial PASSOU com o bug das threads do sqlite presente, e o
servidor real sob carga deu 500 em 278 de 300 requisicoes. Teste que nao
exercita a condicao do defeito nao protege contra ele.
"""
import concurrent.futures
import sqlite3
import shutil
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import agente
from dados import PARAMETROS_REGRAS, aplicar_regras, ranking_clientes_sinalizados
from mesa import api, db, regras_run, repositorio, triagem
from mesa.importar_cache import importar
from mesa.ingestao import ingerir

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"


# ============================================================================
# Stores de teste
# ============================================================================


def _montar(caminho, triar: bool):
    conn = db.conectar(caminho)
    ingerir(conn, DADOS_REAIS)
    regras_run.executar(conn)
    if triar:
        importar(conn)

        def nao_deve_chamar(**kwargs):
            raise AssertionError("chamou a API do LLM: o cache importado cobre os 30")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(agente.CLIENT.chat.completions, "create", nao_deve_chamar)
            triagem.triar(conn, pausa_s=0, verbose=False)
    # checkpoint antes de fechar: garante que o .db copiado depois esta completo,
    # sem depender do arquivo -wal
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


@pytest.fixture(scope="session")
def _template_triado(tmp_path_factory):
    caminho = tmp_path_factory.mktemp("tpl") / "triado.db"
    _montar(caminho, triar=True)
    return caminho


@pytest.fixture(scope="session")
def _template_novo(tmp_path_factory):
    caminho = tmp_path_factory.mktemp("tpl") / "novo.db"
    _montar(caminho, triar=False)
    return caminho


def _cliente(template, tmp_path, monkeypatch) -> TestClient:
    caminho = tmp_path / "mesa.db"
    shutil.copy(template, caminho)
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)
    return TestClient(api.app)


@pytest.fixture
def cliente(_template_triado, tmp_path, monkeypatch):
    """Os 30 alertas triados - o estado depois de uma rodada completa do worker."""
    return _cliente(_template_triado, tmp_path, monkeypatch)


@pytest.fixture
def cliente_novo(_template_novo, tmp_path, monkeypatch):
    """Regras rodadas, nenhum alerta triado ainda - nenhum parecer no store."""
    return _cliente(_template_novo, tmp_path, monkeypatch)


def _alerta_de(cliente, cliente_id: str) -> int:
    itens = cliente.get("/fila?origem=todos&limite=200").json()["itens"]
    return next(i["alerta_id"] for i in itens if i["cliente_id"] == cliente_id)


# ============================================================================
# 2.0 - app, conexao, /saude
# ============================================================================


def test_saude_prova_que_a_api_le_o_store(cliente):
    r = cliente.get("/saude")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["versao_esquema"] == db.VERSAO_ESQUEMA
    assert corpo["contagens"]["operacoes"] == 317
    assert corpo["contagens"]["alertas"] == 30
    assert corpo["execucao_atual"] == 1


def test_store_inexistente_devolve_503_e_nao_cria_banco_vazio(tmp_path, monkeypatch):
    """conectar() criaria um banco vazio em silencio, e a tela mostraria "0 casos"
    como se fosse verdade. A API recusa em vez de inventar."""
    caminho = tmp_path / "nao-existe.db"
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)
    r = TestClient(api.app).get("/saude")
    assert r.status_code == 503
    assert "mesa.ingestao" in r.json()["detail"]
    assert not caminho.exists()


def test_esquema_de_outra_versao_devolve_503(cliente, monkeypatch):
    monkeypatch.setattr(db, "VERSAO_ESQUEMA", db.VERSAO_ESQUEMA + 1)
    r = cliente.get("/saude")
    assert r.status_code == 503
    assert "versão de esquema" in r.json()["detail"]
    assert "python -m mesa.db" in r.json()["detail"]  # aponta a migracao (4.0)


def test_leitura_nao_escreve_no_store(cliente):
    """Uma API de leitura que aplica esquema ou atualiza PRAGMA a cada GET e uma
    API que escreve. O arquivo tem que sair intocado."""
    caminho = db.CAMINHO_PADRAO
    antes = caminho.read_bytes()
    for rota in ["/saude", "/fila", "/fila?origem=todos", "/execucoes"]:
        assert cliente.get(rota).status_code == 200
    assert caminho.read_bytes() == antes


def test_cors_aceita_localhost_e_recusa_origem_externa(cliente):
    local = cliente.get("/saude", headers={"Origin": "http://localhost:5500"})
    assert local.headers.get("access-control-allow-origin") == "http://localhost:5500"

    externa = cliente.get("/saude", headers={"Origin": "https://exemplo.com"})
    assert "access-control-allow-origin" not in externa.headers


# ============================================================================
# 2.1 - GET /fila
# ============================================================================


def test_fila_tem_a_mesma_ordem_do_ranking_das_regras(cliente):
    """<<< aceite do 2.1 >>>"""
    fila = [i["cliente_id"] for i in cliente.get("/fila").json()["itens"]]

    conn = db.conectar(db.CAMINHO_PADRAO)
    df = aplicar_regras(repositorio.operacoes_df(conn))
    conn.close()
    ranking = ranking_clientes_sinalizados(df, top_n=100)["cliente_id"].tolist()

    assert fila == ranking


def test_fila_separa_regra_de_controle(cliente):
    """<<< aceite do 2.1 >>> 17 por padrao, 30 com todos, 13 de controle."""
    assert cliente.get("/fila").json()["total"] == 17
    assert cliente.get("/fila?origem=todos").json()["total"] == 30
    controle = cliente.get("/fila?origem=controle").json()
    assert controle["total"] == 13
    assert all(i["total_sinalizacoes"] == 0 for i in controle["itens"])


def test_fila_reproduz_a_concordancia_do_confronto(cliente):
    """O `concorda` da fila tem que dar os mesmos 23/30 do confronto_resumo.json -
    a API reusa a normalizacao do confronto.py em vez de reescrever."""
    itens = cliente.get("/fila?origem=todos").json()["itens"]
    assert sum(i["concorda"] for i in itens) == 23
    assert sum(i["fundamentado"] for i in itens) == 28


def test_fila_normaliza_o_nivel_do_agente(cliente):
    """O modelo alterna 'medio' e 'médio'. Duas grafias na API empurrariam a
    normalizacao para a tela - uma segunda copia da regra."""
    niveis = {i["nivel_risco_agente"] for i in cliente.get("/fila?origem=todos").json()["itens"]}
    assert niveis <= {"baixo", "médio", "alto"}


def test_sem_parecer_concorda_e_null_nao_false(cliente_novo):
    """"Ainda nao triado" nao e "discordou". A fila tem que distinguir."""
    itens = cliente_novo.get("/fila").json()["itens"]
    assert itens, "fila vazia - o teste nao exercita nada"
    for item in itens:
        assert item["nivel_risco_agente"] is None
        assert item["concorda"] is None
        assert item["fundamentado"] is None


def test_fila_filtra_por_estado(cliente_novo):
    assert cliente_novo.get("/fila?estado=novo").json()["total"] == 17
    assert cliente_novo.get("/fila?estado=triado").json()["total"] == 0


def test_estado_invalido_na_query_e_422(cliente):
    assert cliente.get("/fila?estado=inventado").status_code == 422


def test_paginacao_percorre_a_fila_inteira_sem_repetir(cliente):
    completa = [i["cliente_id"] for i in cliente.get("/fila?origem=todos").json()["itens"]]

    vistos, cursor = [], None
    while True:
        url = "/fila?origem=todos&limite=7" + (f"&cursor={cursor}" if cursor else "")
        pagina = cliente.get(url).json()
        vistos += [i["cliente_id"] for i in pagina["itens"]]
        cursor = pagina["proximo_cursor"]
        if cursor is None:
            break

    assert vistos == completa
    assert len(set(vistos)) == 30


def test_paginacao_nao_pula_caso_quando_a_fila_muda_entre_paginas(cliente):
    """O motivo de o cursor ser por CHAVE e nao por offset. Um analista pega um
    caso da 1a pagina enquanto outro le a 2a: com offset, o caso some do filtro,
    tudo desloca uma posicao e um item e PULADO sem ninguem perceber."""
    p1 = cliente.get("/fila?estado=triado&limite=5").json()
    esperado_restante = [
        i["cliente_id"] for i in cliente.get("/fila?estado=triado").json()["itens"]
    ][5:]

    pego = p1["itens"][0]["alerta_id"]
    r = cliente.post(f"/alertas/{pego}/estado", json={"estado": "em_analise"},
                     headers={"X-Analista": "ana"})
    assert r.status_code == 200

    p2 = cliente.get(f"/fila?estado=triado&limite=50&cursor={p1['proximo_cursor']}").json()
    assert [i["cliente_id"] for i in p2["itens"]] == esperado_restante


def test_cursor_invalido_e_400(cliente):
    r = cliente.get("/fila?cursor=isto-nao-e-um-cursor")
    assert r.status_code == 400
    assert "cursor" in r.json()["detail"]


def test_limite_fora_da_faixa_e_422(cliente):
    assert cliente.get("/fila?limite=0").status_code == 422
    assert cliente.get("/fila?limite=10000").status_code == 422


def test_execucao_inexistente_na_fila_e_404(cliente):
    assert cliente.get("/fila?execucao_id=999").status_code == 404


# ============================================================================
# 2.2 - GET /alertas/{id}
# ============================================================================


def test_alerta_inexistente_e_404(cliente):
    """<<< aceite do 2.2 >>>"""
    r = cliente.get("/alertas/99999")
    assert r.status_code == 404
    assert r.json() == {"detail": "alerta 99999 não existe"}


def test_caso_com_fracionamento_traz_a_data_que_disparou(cliente):
    """<<< aceite do 2.2 >>>"""
    conn = db.conectar(db.CAMINHO_PADRAO)
    cliente_frac = conn.execute(
        "SELECT cliente_id FROM sinalizacoes WHERE regra='fracionamento' LIMIT 1"
    ).fetchone()[0]
    esperadas = regras_run.datas_fracionamento_do_store(conn, cliente_frac)
    conn.close()

    caso = cliente.get(f"/alertas/{_alerta_de(cliente, cliente_frac)}").json()
    frac = [s for s in caso["sinalizacoes"] if s["regra"] == "fracionamento"]
    assert [s["data"] for s in frac] == esperadas
    assert frac[0]["operacao_id"] is None
    assert frac[0]["detalhe"]["soma_do_dia"] > PARAMETROS_REGRAS["frac_soma_min"]


def test_flags_das_operacoes_vem_das_sinalizacoes_gravadas(cliente):
    """A API marca a operacao atipica pelo que as regras GRAVARAM, nao por
    recalculo. As marcas tem que coincidir uma a uma com as sinalizacoes."""
    caso = cliente.get(f"/alertas/{_alerta_de(cliente, 'CLI-028')}").json()
    marcadas = {o["id"] for o in caso["operacoes"] if o["flag_valor_atipico"]}
    sinalizadas = {s["operacao_id"] for s in caso["sinalizacoes"] if s["regra"] == "valor_atipico"}
    assert marcadas == sinalizadas
    assert len(marcadas) == 2  # as duas de R$ 27.715,48 e R$ 24.875,39


def test_caso_cli_028_mostra_o_erro_que_a_aderencia_pegou(cliente):
    """O caso da apresentacao: o parecer chama de atipica uma operacao que nao e.
    A API tem que entregar a tela tudo que ela precisa para mostrar isso."""
    caso = cliente.get(f"/alertas/{_alerta_de(cliente, 'CLI-028')}").json()
    assert caso["aderencia"]["fundamentado"] is False
    assert [d["valor"] for d in caso["aderencia"]["atipicos_incorretos"]] == [6913.84]
    citada = next(o for o in caso["operacoes"] if abs(o["valor_brl"] - 6913.84) < 0.01)
    assert citada["flag_valor_atipico"] is False


def test_historico_expoe_as_versoes_anteriores_do_parecer(cliente):
    """O append-only visivel: CLI-014 tem pareceres importados de versoes
    antigas do prompt, e o analista tem que poder ve-los."""
    caso = cliente.get(f"/alertas/{_alerta_de(cliente, 'CLI-014')}").json()
    historico = caso["historico_parecer"]
    assert len(historico) > 1
    assert {h["origem_registro"] for h in historico} == {"importado_cache"}
    assert historico[0]["parecer_id"] == caso["parecer"]["parecer_id"]  # o mais recente e o atual


def test_caso_sem_parecer_devolve_parecer_e_aderencia_null(cliente_novo):
    caso = cliente_novo.get(f"/alertas/{_alerta_de(cliente_novo, 'CLI-014')}").json()
    assert caso["parecer"] is None
    assert caso["aderencia"] is None
    assert caso["historico_parecer"] == []
    assert len(caso["operacoes"]) == 11


def test_operacao_sem_data_vem_no_fim(cliente):
    conn = db.conectar(db.CAMINHO_PADRAO)
    com_nula = conn.execute(
        "SELECT cliente_id FROM operacoes WHERE data IS NULL LIMIT 1"
    ).fetchone()[0]
    conn.close()

    datas = [o["data"] for o in
             cliente.get(f"/alertas/{_alerta_de(cliente, com_nula)}").json()["operacoes"]]
    primeira_nula = datas.index(None)
    assert all(d is None for d in datas[primeira_nula:])


# ============================================================================
# 2.3 - GET /alertas/{id}/evidencias
# ============================================================================


def test_alerta_reaproveitado_tambem_devolve_evidencia(cliente):
    """<<< aceite do 2.3 >>> A copia do passo 1.5 carrega as evidencias junto."""
    caso = cliente.get(f"/alertas/{_alerta_de(cliente, 'CLI-014')}").json()
    assert caso["parecer"]["reaproveitado_de"] is not None

    ev = cliente.get(f"/alertas/{caso['alerta']['alerta_id']}/evidencias").json()
    assert len(ev) >= 1
    assert ev[0]["tool"] == "historico_cliente"
    assert ev[0]["args"] == {"cliente_id": "CLI-014"}


def test_evidencia_importada_nao_tem_payload(cliente):
    """Fato do historico, nao bug: o cache antigo nunca guardou o retorno das
    ferramentas (so a partir do passo 1.4). A API devolve null em vez de
    inventar um payload."""
    ev = cliente.get(f"/alertas/{_alerta_de(cliente, 'CLI-014')}/evidencias").json()
    assert all(e["payload"] is None for e in ev)


def test_alerta_sem_parecer_devolve_lista_vazia(cliente_novo):
    """<<< aceite do 2.3 >>> O alerta existe, so nao foi triado: [] e nao 404."""
    r = cliente_novo.get(f"/alertas/{_alerta_de(cliente_novo, 'CLI-014')}/evidencias")
    assert r.status_code == 200
    assert r.json() == []


def test_evidencias_de_alerta_inexistente_e_404(cliente):
    assert cliente.get("/alertas/99999/evidencias").status_code == 404


# ============================================================================
# 2.4 - POST /alertas/{id}/estado   e   2.5 - X-Analista
# ============================================================================


def _mudar(cliente, alerta_id, estado, analista="ana"):
    headers = {"X-Analista": analista} if analista is not None else {}
    return cliente.post(f"/alertas/{alerta_id}/estado", json={"estado": estado}, headers=headers)


def _estado(cliente, alerta_id):
    return cliente.get(f"/alertas/{alerta_id}").json()["alerta"]


def test_novo_para_concluido_e_409_e_nao_altera_o_estado(cliente_novo):
    """<<< aceite do 2.4 >>>"""
    aid = _alerta_de(cliente_novo, "CLI-014")
    r = _mudar(cliente_novo, aid, "concluido")
    assert r.status_code == 409
    assert _estado(cliente_novo, aid)["estado"] == "novo"


def test_estado_nao_conclui_caso_nem_vindo_de_em_analise(cliente):
    """Ambiguidade do ROADMAP resolvida na 2.4, e mantida na Fase 4: concluir so
    pelo POST /decisao, que grava a decisao junto. Pelo /estado, fecharia um
    caso sem registro do que o analista decidiu."""
    aid = _alerta_de(cliente, "CLI-014")
    assert _mudar(cliente, aid, "em_analise").status_code == 200

    r = _mudar(cliente, aid, "concluido")
    assert r.status_code == 409
    assert f"/alertas/{aid}/decisao" in r.json()["detail"]
    assert _estado(cliente, aid)["estado"] == "em_analise"


def test_pegar_caso_triado_grava_o_analista(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    r = _mudar(cliente, aid, "em_analise", analista="ana.souza")
    assert r.status_code == 200
    assert r.json() == {
        "alerta_id": aid, "estado_anterior": "triado",
        "estado": "em_analise", "analista_id": "ana.souza",
    }
    assert _estado(cliente, aid)["analista_id"] == "ana.souza"


def test_devolver_para_a_fila_libera_o_dono(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    r = _mudar(cliente, aid, "triado")
    assert r.status_code == 200
    depois = _estado(cliente, aid)
    assert depois["estado"] == "triado"
    assert depois["analista_id"] is None


def test_caso_ja_em_analise_nao_pode_ser_pego_de_novo(cliente):
    """Sem esta recusa, um segundo analista "roubaria" o caso em silencio."""
    aid = _alerta_de(cliente, "CLI-014")
    assert _mudar(cliente, aid, "em_analise", analista="ana").status_code == 200

    r = _mudar(cliente, aid, "em_analise", analista="bruno")
    assert r.status_code == 409
    # a mensagem diz QUEM pegou - e o que o analista com a tela desatualizada
    # precisa saber, nao o nome tecnico da transicao
    assert r.json()["detail"] == "o caso CLI-014 já está em análise com ana"
    assert _estado(cliente, aid)["analista_id"] == "ana"


def test_so_quem_pegou_pode_devolver(cliente):
    """Sem esta regra, qualquer analista com a pagina aberta devolveria para a
    fila um caso em analise por outro - e o dono nem ficaria sabendo."""
    aid = _alerta_de(cliente, "CLI-014")
    assert _mudar(cliente, aid, "em_analise", analista="ana").status_code == 200

    r = _mudar(cliente, aid, "triado", analista="bruno")
    assert r.status_code == 409
    assert "ana" in r.json()["detail"]
    depois = _estado(cliente, aid)
    assert (depois["estado"], depois["analista_id"]) == ("em_analise", "ana")

    assert _mudar(cliente, aid, "triado", analista="ana").status_code == 200


def test_api_nao_duplica_o_caminho_do_worker(cliente_novo):
    """novo -> triado e escrito pelo worker, que rodou o agente. A API nao pode
    marcar como triado um caso que nenhum agente viu."""
    aid = _alerta_de(cliente_novo, "CLI-014")
    assert _mudar(cliente_novo, aid, "triado").status_code == 409


def test_mensagem_de_409_diz_o_que_era_permitido(cliente_novo):
    r = _mudar(cliente_novo, _alerta_de(cliente_novo, "CLI-014"), "triado")
    assert "em_analise" in r.json()["detail"]


def test_mudar_estado_de_alerta_inexistente_e_404(cliente):
    assert _mudar(cliente, 99999, "em_analise").status_code == 404


def test_estado_de_destino_invalido_e_422(cliente):
    assert _mudar(cliente, _alerta_de(cliente, "CLI-014"), "arquivado").status_code == 422


def test_sem_x_analista_e_400(cliente):
    """<<< aceite do 2.5 >>>"""
    aid = _alerta_de(cliente, "CLI-014")
    r = _mudar(cliente, aid, "em_analise", analista=None)
    assert r.status_code == 400
    assert "X-Analista" in r.json()["detail"]
    assert _estado(cliente, aid)["estado"] == "triado"


def test_x_analista_em_branco_tambem_e_400(cliente):
    assert _mudar(cliente, _alerta_de(cliente, "CLI-014"), "em_analise", analista="   ").status_code == 400


def test_get_nao_exige_x_analista(cliente):
    """<<< aceite do 2.5 >>> Obrigatorio so onde ha escrita."""
    assert cliente.get("/fila").status_code == 200


# ============================================================================
# 2.6 - GET /execucoes
# ============================================================================


def test_execucao_devolve_os_parametros_gravados_nao_os_do_codigo(cliente):
    """<<< aceite do 2.6 >>> Roda uma segunda execucao com outro limiar: a API
    tem que devolver o que cada uma usou, nao o PARAMETROS_REGRAS de hoje."""
    conn = db.conectar(db.CAMINHO_PADRAO)
    regras_run.executar(conn, {**PARAMETROS_REGRAS, "atipico_fator": 10})
    conn.close()

    execucoes = cliente.get("/execucoes").json()
    assert [e["parametros"]["atipico_fator"] for e in execucoes] == [10, 5]  # mais recente primeiro

    primeira = cliente.get("/execucoes/1").json()
    assert primeira["parametros"] == PARAMETROS_REGRAS
    assert (primeira["alertas_regra"], primeira["alertas_controle"]) == (17, 13)


def test_execucao_inexistente_e_404(cliente):
    assert cliente.get("/execucoes/999").status_code == 404


# ============================================================================
# Concorrencia - com servidor de verdade
# ============================================================================


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def servidor(_template_triado, tmp_path, monkeypatch):
    caminho = tmp_path / "mesa.db"
    shutil.copy(_template_triado, caminho)
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)

    porta = _porta_livre()
    srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=porta, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    limite = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < limite, "uvicorn nao subiu em 10s"
        time.sleep(0.05)

    yield f"http://127.0.0.1:{porta}"

    srv.should_exit = True
    thread.join(timeout=5)


def test_leituras_concorrentes_nao_quebram(servidor):
    """Regressao do bug medido: com check_same_thread no padrao, 278 de 300
    requisicoes concorrentes deram 500. O TestClient sequencial nao pega isso."""
    def get(_):
        return httpx.get(f"{servidor}/fila?origem=todos", timeout=10).status_code

    with concurrent.futures.ThreadPoolExecutor(32) as ex:
        status = list(ex.map(get, range(300)))

    assert status.count(200) == 300, {s: status.count(s) for s in set(status)}


def test_dois_analistas_nao_pegam_o_mesmo_caso(servidor):
    """20 analistas clicam em "pegar caso" ao mesmo tempo. Exatamente um vence;
    os outros recebem 409.

    Este teste e PROBABILISTICO: depende de as threads se intercalarem no
    momento certo. Reintroduzindo o UPDATE sem condicao, ele falhou em 2 de 3
    execucoes - e passou na terceira com o bug presente. Fica porque exercita o
    cenario real sob carga, mas quem PROTEGE contra a regressao e o teste
    deterministico logo abaixo."""
    alerta_id = next(
        i["alerta_id"] for i in httpx.get(f"{servidor}/fila").json()["itens"]
        if i["cliente_id"] == "CLI-014"
    )

    def pegar(n):
        return httpx.post(
            f"{servidor}/alertas/{alerta_id}/estado", json={"estado": "em_analise"},
            headers={"X-Analista": f"analista-{n}"}, timeout=10,
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(20) as ex:
        status = list(ex.map(pegar, range(20)))

    assert status.count(200) == 1, status
    assert status.count(409) == 19, status
    dono = httpx.get(f"{servidor}/alertas/{alerta_id}").json()["alerta"]["analista_id"]
    assert dono.startswith("analista-")


def test_estado_que_muda_entre_a_leitura_e_a_escrita_e_409(cliente, monkeypatch):
    """Versao DETERMINISTICA da corrida acima - nao depende de sorte de thread.

    Forca a intercalacao exata: a requisicao le o estado ('triado'), OUTRO
    analista pega o caso nesse intervalo, e so entao o UPDATE roda. Com o
    compare-and-set, o UPDATE nao encontra mais 'triado' e devolve 409. Com um
    UPDATE sem condicao, o segundo analista sobrescreveria o primeiro em
    silencio - e este teste falharia sempre, nao 2 vezes em 3."""
    aid = _alerta_de(cliente, "CLI-014")
    ler_original = api._alerta_ou_404

    def ler_e_deixar_outro_analista_passar_na_frente(conn, alerta_id):
        linha = ler_original(conn, alerta_id)  # le 'triado'
        outra = db.conectar(db.CAMINHO_PADRAO)
        outra.execute("UPDATE alertas SET estado='em_analise', analista_id='bruno' "
                      "WHERE id = ?", (alerta_id,))
        outra.commit()
        outra.close()
        return linha  # devolve a leitura ja desatualizada

    monkeypatch.setattr(api, "_alerta_ou_404", ler_e_deixar_outro_analista_passar_na_frente)
    r = _mudar(cliente, aid, "em_analise", analista="ana")
    monkeypatch.setattr(api, "_alerta_ou_404", ler_original)

    assert r.status_code == 409
    assert "mudou de estado" in r.json()["detail"]
    assert _estado(cliente, aid)["analista_id"] == "bruno"  # quem chegou primeiro fica


# ============================================================================
# Fase 3 - o que a API passou a entregar para a tela
# ============================================================================


def test_marcas_apontam_para_o_trecho_exato_da_justificativa(cliente):
    """As posicoes gravadas pelo verificador sobrevivem ao store e a API: o
    trecho justificativa[inicio:fim] e exatamente o numero que o leitor ve."""
    caso = cliente.get(f"/alertas/{_alerta_de(cliente, 'CLI-028')}").json()
    texto = caso["parecer"]["justificativa"]
    marcas = caso["aderencia"]["marcas"]

    errada = next(m for m in marcas if m["classe"] == "atipico_incorreto")
    assert texto[errada["inicio"]:errada["fim"]] == "R$6.913,84"
    assert errada["fonte"] == "operacao OP-00269"


def test_marcas_de_todos_os_casos_caem_dentro_do_texto(cliente):
    """Nenhuma marca pode apontar para fora da justificativa, nem se sobrepor."""
    for item in cliente.get("/fila?origem=todos&limite=200").json()["itens"]:
        caso = cliente.get(f"/alertas/{item['alerta_id']}").json()
        if not caso["parecer"] or not caso["aderencia"]:
            continue
        texto = caso["parecer"]["justificativa"]
        fim_anterior = 0
        for m in caso["aderencia"]["marcas"]:
            assert 0 <= m["inicio"] < m["fim"] <= len(texto), item["cliente_id"]
            assert m["inicio"] >= fim_anterior, f"marcas sobrepostas em {item['cliente_id']}"
            assert "R$" in texto[m["inicio"]:m["fim"]] or "BRL" in texto[m["inicio"]:m["fim"]]
            fim_anterior = m["fim"]


def test_mediana_marcada_como_agregado_e_nao_como_operacao(cliente):
    """<<< aceite do 3.2 >>> CLI-014 tem 11 operacoes: a mediana e o valor exato
    de uma operacao real (OP-00127). A marca tem que apontar para a mediana."""
    caso = cliente.get(f"/alertas/{_alerta_de(cliente, 'CLI-014')}").json()
    texto = caso["parecer"]["justificativa"]
    mediana = next(m for m in caso["aderencia"]["marcas"] if "2.308,41" in texto[m["inicio"]:m["fim"]])
    assert mediana["fonte"] == "mediana_cliente"
    assert any(abs(o["valor_brl"] - 2308.41) < 0.01 for o in caso["operacoes"])  # a armadilha existe


def test_transicoes_permitidas_seguem_a_mesma_tabela_do_post(cliente):
    """A tela mostra so os botoes que a API diz. Se esta lista e a do POST
    divergissem, a tela ofereceria um botao que sempre da 409."""
    aid = _alerta_de(cliente, "CLI-014")
    assert cliente.get(f"/alertas/{aid}").json()["transicoes_permitidas"] == ["em_analise"]

    _mudar(cliente, aid, "em_analise")
    assert cliente.get(f"/alertas/{aid}").json()["transicoes_permitidas"] == ["triado"]

    for destino in cliente.get(f"/alertas/{aid}").json()["transicoes_permitidas"]:
        assert _mudar(cliente, aid, destino).status_code == 200



# ============================================================================
# Fase 4.1 - decisao do analista e trilha de transicoes
# ============================================================================


def _decidir(cliente, alerta_id, decisao, analista="ana", parecer_id="atual", **extra):
    if parecer_id == "atual":
        parecer = cliente.get(f"/alertas/{alerta_id}").json()["parecer"]
        parecer_id = parecer["parecer_id"] if parecer else None
    headers = {"X-Analista": analista} if analista is not None else {}
    corpo = {"decisao": decisao, "parecer_id": parecer_id, **extra}
    return cliente.post(f"/alertas/{alerta_id}/decisao", json=corpo, headers=headers)


def _trilha(cliente, alerta_id):
    return [(t["estado_anterior"], t["estado_novo"], t["ator"])
            for t in cliente.get(f"/alertas/{alerta_id}").json()["trilha"]]


def test_discordar_com_motivo_conclui_o_caso_com_decisao_e_trilha(cliente):
    """<<< aceite do 4.1 (lado da API) >>> CLI-028: o parecer cita R$6.913,84
    como atipico, e nao e. O analista discorda - e isso fica registrado."""
    aid = _alerta_de(cliente, "CLI-028")
    assert _mudar(cliente, aid, "em_analise").status_code == 200

    r = _decidir(cliente, aid, "discordo", nivel_risco="alto",
                 motivo="parecer cita OP-00269 como atipica; nao e")
    assert r.status_code == 200, r.json()

    caso = cliente.get(f"/alertas/{aid}").json()
    assert caso["alerta"]["estado"] == "concluido"
    assert caso["alerta"]["decisao"] == "discordo"
    d = caso["decisao"]
    assert (d["decisao"], d["analista_id"], d["nivel_risco_analista"]) == ("discordo", "ana", "alto")
    assert d["parecer_id"] == caso["parecer"]["parecer_id"]
    assert d["nivel_risco_agente"] == caso["parecer"]["nivel_risco"]
    assert _trilha(cliente, aid) == [
        ("novo", "triado", "sistema:triagem"),
        ("triado", "em_analise", "ana"),
        ("em_analise", "concluido", "ana"),
    ]
    assert caso["transicoes_permitidas"] == []  # concluido e terminal
    fila = cliente.get("/fila?estado=concluido").json()["itens"]
    assert [(i["cliente_id"], i["decisao"]) for i in fila] == [("CLI-028", "discordo")]


def test_concordar_grava_o_nivel_do_agente(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    agente_nivel = cliente.get(f"/alertas/{aid}").json()["parecer"]["nivel_risco"]

    r = _decidir(cliente, aid, "concordo")
    assert r.status_code == 200
    assert r.json()["nivel_risco_analista"] == agente_nivel == r.json()["nivel_risco_agente"]


def test_concordar_com_nivel_diferente_do_agente_e_422(cliente):
    """'Concordo, mas o nivel e outro' e uma discordancia - tem que ter motivo."""
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    agente_nivel = cliente.get(f"/alertas/{aid}").json()["parecer"]["nivel_risco"]
    outro = next(n for n in ("baixo", "médio", "alto") if n != agente_nivel)

    r = _decidir(cliente, aid, "concordo", nivel_risco=outro)
    assert r.status_code == 422
    assert "discordo" in r.json()["detail"]
    assert _estado(cliente, aid)["estado"] == "em_analise"


def test_concordar_sem_parecer_e_422(cliente_novo):
    """<<< aceite do 4.1 >>> Caso pego antes da triagem: nao ha com que concordar."""
    aid = _alerta_de(cliente_novo, "CLI-014")
    _mudar(cliente_novo, aid, "em_analise")
    r = _decidir(cliente_novo, aid, "concordo", parecer_id=None)
    assert r.status_code == 422
    assert _estado(cliente_novo, aid)["estado"] == "em_analise"


def test_caso_sem_parecer_pode_ser_decidido_discordando(cliente_novo):
    """O LLM falhar (ou nao ter rodado) nao pode travar o analista."""
    aid = _alerta_de(cliente_novo, "CLI-014")
    _mudar(cliente_novo, aid, "em_analise")
    r = _decidir(cliente_novo, aid, "discordo", parecer_id=None,
                 nivel_risco="alto", motivo="fracionamento evidente nas operacoes")
    assert r.status_code == 200
    assert (r.json()["parecer_id"], r.json()["nivel_risco_agente"]) == (None, None)


def test_outro_analista_nao_decide_o_caso(cliente):
    """<<< aceite do 4.1 >>>"""
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise", analista="ana")
    r = _decidir(cliente, aid, "escalar", analista="bruno", motivo="grave")
    assert r.status_code == 409
    assert "ana" in r.json()["detail"]
    assert _estado(cliente, aid)["estado"] == "em_analise"
    assert cliente.get(f"/alertas/{aid}").json()["decisao"] is None


@pytest.mark.parametrize("corpo,trecho", [
    ({"decisao": "discordo", "nivel_risco": "alto"}, "motivo"),
    ({"decisao": "discordo", "motivo": "x"}, "nível"),
    ({"decisao": "discordo", "nivel_risco": "alto", "motivo": "   "}, "motivo"),
    ({"decisao": "escalar"}, "motivo"),
    ({"decisao": "discordo", "nivel_risco": "altíssimo", "motivo": "x"}, "inválido"),
])
def test_corpo_invalido_para_a_decisao_e_422(cliente, corpo, trecho):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    r = _decidir(cliente, aid, **corpo)
    assert r.status_code == 422
    assert trecho in r.json()["detail"]
    assert _estado(cliente, aid)["estado"] == "em_analise"


def test_decisao_desconhecida_e_422(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    assert _decidir(cliente, aid, "arquivar").status_code == 422


def test_nivel_sem_acento_e_normalizado(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    r = _decidir(cliente, aid, "discordo", nivel_risco="Medio", motivo="x")
    assert r.status_code == 200
    assert r.json()["nivel_risco_analista"] == "médio"


def test_escalar_sem_nivel_e_aceito(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    r = _decidir(cliente, aid, "escalar", motivo="contraparte recorrente em outros casos")
    assert r.status_code == 200
    assert r.json()["nivel_risco_analista"] is None


def test_parecer_diferente_do_que_o_analista_viu_e_409(cliente):
    """A decisao grava o parecer VISTO. Se o atual e outro, o analista decidiu
    sobre algo que nao leu."""
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    atual = cliente.get(f"/alertas/{aid}").json()["parecer"]["parecer_id"]
    r = _decidir(cliente, aid, "concordo", parecer_id=atual - 1)
    assert r.status_code == 409
    assert "parecer" in r.json()["detail"]


def test_parecer_id_e_obrigatorio_no_corpo(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    r = cliente.post(f"/alertas/{aid}/decisao", json={"decisao": "escalar", "motivo": "x"},
                     headers={"X-Analista": "ana"})
    assert r.status_code == 422


def test_caso_que_ninguem_pegou_nao_e_decidido(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    r = _decidir(cliente, aid, "escalar", motivo="x")
    assert r.status_code == 409
    assert "pegue o caso" in r.json()["detail"]


def test_caso_decidido_nao_e_decidido_de_novo(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    assert _decidir(cliente, aid, "escalar", motivo="x").status_code == 200
    r = _decidir(cliente, aid, "concordo")
    assert r.status_code == 409
    assert "já foi decidido" in r.json()["detail"]
    # e nao volta para a fila pelo /estado
    assert _mudar(cliente, aid, "triado").status_code == 409


def test_decidir_sem_x_analista_e_400(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    assert _decidir(cliente, aid, "escalar", analista=None, motivo="x").status_code == 400


def test_falha_no_meio_da_decisao_nao_deixa_nada_gravado(cliente, monkeypatch):
    """<<< aceite do 4.1 >>> A trilha falha DEPOIS de o estado mudar e a decisao
    ser inserida. Com as tres escritas na mesma transacao, nada fica: o caso
    continua em analise, sem decisao - nao existe caso concluido sem registro."""
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    trilha_antes = _trilha(cliente, aid)

    def trilha_que_falha(*args, **kwargs):
        raise sqlite3.OperationalError("disco cheio simulado")

    registrar_original = api._registrar_transicao
    monkeypatch.setattr(api, "_registrar_transicao", trilha_que_falha)
    r = TestClient(api.app, raise_server_exceptions=False).post(
        f"/alertas/{aid}/decisao",
        json={"decisao": "escalar", "motivo": "x",
              "parecer_id": cliente.get(f"/alertas/{aid}").json()["parecer"]["parecer_id"]},
        headers={"X-Analista": "ana"},
    )
    # NAO monkeypatch.undo(): desfaria tambem o CAMINHO_PADRAO do store de teste
    monkeypatch.setattr(api, "_registrar_transicao", registrar_original)
    assert r.status_code == 500

    caso = cliente.get(f"/alertas/{aid}").json()
    assert (caso["alerta"]["estado"], caso["alerta"]["analista_id"]) == ("em_analise", "ana")
    assert caso["decisao"] is None
    assert _trilha(cliente, aid) == trilha_antes


def test_decisao_duplicada_na_corrida_e_409(cliente, monkeypatch):
    """Mesmo analista, duas abas, dois cliques: entre a leitura e a escrita da
    primeira requisicao, a segunda ja concluiu o caso."""
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise")
    parecer_id = cliente.get(f"/alertas/{aid}").json()["parecer"]["parecer_id"]
    ler_original = api._alerta_ou_404

    def ler_e_deixar_a_outra_aba_concluir(conn, alerta_id):
        linha = ler_original(conn, alerta_id)  # le em_analise
        outra = db.conectar(db.CAMINHO_PADRAO)
        outra.execute("UPDATE alertas SET estado='concluido' WHERE id = ?", (alerta_id,))
        outra.execute("INSERT INTO decisoes (alerta_id, parecer_id, analista_id, decisao, motivo, "
                      "decidido_em) VALUES (?, ?, 'ana', 'escalar', 'primeira aba', 'x')",
                      (alerta_id, parecer_id))
        outra.commit()
        outra.close()
        return linha

    monkeypatch.setattr(api, "_alerta_ou_404", ler_e_deixar_a_outra_aba_concluir)
    r = _decidir(cliente, aid, "escalar", parecer_id=parecer_id, motivo="segunda aba")
    monkeypatch.setattr(api, "_alerta_ou_404", ler_original)

    assert r.status_code == 409
    assert cliente.get(f"/alertas/{aid}").json()["decisao"]["motivo"] == "primeira aba"


def test_pegar_e_devolver_ficam_na_trilha(cliente):
    """<<< o que a Fase 2 observou >>> Antes, quem pegou e devolveu sumia."""
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise", analista="ana")
    _mudar(cliente, aid, "triado", analista="ana")
    _mudar(cliente, aid, "em_analise", analista="bruno")
    assert _trilha(cliente, aid) == [
        ("novo", "triado", "sistema:triagem"),
        ("triado", "em_analise", "ana"),
        ("em_analise", "triado", "ana"),
        ("triado", "em_analise", "bruno"),
    ]


def test_transicao_recusada_nao_entra_na_trilha(cliente):
    aid = _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid, "em_analise", analista="ana")
    antes = _trilha(cliente, aid)
    assert _mudar(cliente, aid, "em_analise", analista="bruno").status_code == 409
    assert _mudar(cliente, aid, "triado", analista="bruno").status_code == 409
    assert _trilha(cliente, aid) == antes


# ============================================================================
# Fase 4.2 / 4.3 - metricas
# ============================================================================


def test_sem_decisao_as_metricas_sao_null_e_nao_zero(cliente):
    """<<< aceite do 4.2 >>> "Ninguem decidiu nada" nao e "0% de concordancia"."""
    m = cliente.get("/metricas").json()
    assert m["decididos"] == 0
    assert m["agente_vs_analista"]["concordancia"] is None
    assert m["agente_vs_analista"]["aceitacao_do_parecer"] is None
    assert m["aderencia_vs_decisao"]["nao_fundamentado"]["taxa_de_rejeicao"] is None
    assert m["tempo"]["mediana_s"] is None
    # a matriz vem completa mesmo vazia: celula zerada e informacao
    assert m["agente_vs_analista"]["matriz"]["alto"] == {"baixo": 0, "médio": 0, "alto": 0}


def test_metricas_de_decisoes_fabricadas_conferidas_a_mao(cliente):
    """<<< aceite do 4.2 >>> Cinco decisoes sobre casos reais, numeros esperados
    calculados a mao ANTES de rodar:

      caso     regra  agente  fund.  decisao                -> analista
      CLI-014  alto   medio   sim    concordo               -> medio
      CLI-028  alto   medio   NAO    discordo               -> alto
      CLI-001  alto   alto    NAO    escalar (sem nivel)    -> -
      CLI-021  medio  alto    sim    discordo               -> medio
      CLI-011  baixo  baixo   sim    concordo               -> baixo
    """
    def fechar(cliente_id, decisao, **extra):
        aid = _alerta_de(cliente, cliente_id)
        assert _mudar(cliente, aid, "em_analise").status_code == 200
        r = _decidir(cliente, aid, decisao, **extra)
        assert r.status_code == 200, r.json()

    fechar("CLI-014", "concordo")
    fechar("CLI-028", "discordo", nivel_risco="alto", motivo="OP-00269 nao e atipica")
    fechar("CLI-001", "escalar", motivo="parecer sem numero nenhum")
    fechar("CLI-021", "discordo", nivel_risco="médio", motivo="uma atipica so")
    fechar("CLI-011", "concordo")

    m = cliente.get("/metricas").json()
    assert (m["casos"], m["decididos"]) == (30, 5)
    assert m["por_decisao"] == {"concordo": 2, "discordo": 2, "escalar": 1}

    av = m["agente_vs_analista"]
    # CLI-001 fica fora: escalou sem nivel. Iguais: CLI-014 e CLI-011 -> 2/4
    assert (av["comparaveis"], av["concordancia"]) == (4, 0.5)
    assert (av["decididos_com_parecer"], av["aceitacao_do_parecer"]) == (5, 0.4)
    assert av["matriz"]["médio"] == {"baixo": 0, "médio": 1, "alto": 1}
    assert av["matriz"]["alto"] == {"baixo": 0, "médio": 1, "alto": 0}
    assert av["matriz"]["baixo"] == {"baixo": 1, "médio": 0, "alto": 0}

    # regra x analista: 014 (alto/medio) x, 028 (alto/alto) ok, 021 (medio/medio) ok,
    # 011 ok -> 3/4. regra x agente, nos mesmos 5: so 001 e 011 batem -> 2/5
    assert (m["regra_vs_analista"]["comparaveis"], m["regra_vs_analista"]["concordancia"]) == (4, 0.75)
    assert (m["regra_vs_agente"]["comparaveis"], m["regra_vs_agente"]["concordancia"]) == (5, 0.4)

    # o verificador: os 2 pareceres nao fundamentados foram ambos rejeitados;
    # dos 3 fundamentados, 1 (CLI-021)
    ad = m["aderencia_vs_decisao"]
    assert ad["nao_fundamentado"] == {"decididos": 2, "concordo": 0, "discordo": 1,
                                      "escalar": 1, "taxa_de_rejeicao": 1.0}
    assert ad["fundamentado"]["decididos"] == 3
    assert ad["fundamentado"]["taxa_de_rejeicao"] == pytest.approx(1 / 3)
    assert ad["sem_verificacao"]["taxa_de_rejeicao"] is None


def _fechar_com_trilha(conn, cliente_id, decisao, passos):
    """Grava direto no store uma trilha com horarios CONHECIDOS - pela API, os
    horarios seriam 'agora' e a soma nao seria conferivel a mao."""
    aid, parecer_id, nivel = conn.execute(
        "SELECT a.id, p.id, p.nivel_risco FROM alertas a JOIN pareceres p ON p.alerta_id = a.id "
        "WHERE a.cliente_id = ? ORDER BY p.criado_em DESC, p.id DESC LIMIT 1", (cliente_id,)
    ).fetchone()
    for de, para, ator, hora in passos:
        conn.execute("INSERT INTO transicoes (alerta_id, estado_anterior, estado_novo, ator, "
                     "ator_tipo, registrado_em) VALUES (?, ?, ?, ?, 'analista', ?)",
                     (aid, de, para, ator, f"2026-09-27T{hora}Z"))
    conn.execute("UPDATE alertas SET estado = 'concluido' WHERE id = ?", (aid,))
    nivel = _normalizar(nivel)
    conn.execute("INSERT INTO decisoes (alerta_id, parecer_id, analista_id, decisao, "
                 "nivel_risco_analista, nivel_risco_agente, motivo, decidido_em) "
                 "VALUES (?, ?, 'x', ?, ?, ?, 'm', '2026-09-27T12:00:00Z')",
                 (aid, parecer_id, decisao, "alto" if decisao == "discordo" else nivel, nivel))


def _normalizar(nivel):
    from confronto import _normalizar_nivel
    return _normalizar_nivel(nivel)


def test_tempo_de_analise_soma_os_periodos_da_trilha(cliente):
    """<<< aceite do 4.3 >>>
      CLI-014: ana pega 10:00, devolve 10:10; bruno pega 11:00, conclui 11:05
               -> 10 + 5 = 15 min = 900 s (os dois periodos contam)
      CLI-023: pega 09:00, conclui 09:20 -> 1200 s
      CLI-013: so a conclusao, sem a entrada (pego antes da trilha) -> nao medido
    """
    conn = db.conectar(db.CAMINHO_PADRAO)
    _fechar_com_trilha(conn, "CLI-014", "concordo", [
        ("triado", "em_analise", "ana", "10:00:00"),
        ("em_analise", "triado", "ana", "10:10:00"),
        ("triado", "em_analise", "bruno", "11:00:00"),
        ("em_analise", "concluido", "bruno", "11:05:00"),
    ])
    _fechar_com_trilha(conn, "CLI-023", "discordo", [
        ("triado", "em_analise", "ana", "09:00:00"),
        ("em_analise", "concluido", "ana", "09:20:00"),
    ])
    _fechar_com_trilha(conn, "CLI-013", "concordo", [
        ("em_analise", "concluido", "ana", "09:30:00"),
    ])
    conn.commit()
    conn.close()

    t = cliente.get("/metricas").json()["tempo"]
    assert (t["casos_medidos"], t["casos_sem_trilha_completa"]) == (2, 1)
    assert (t["mediana_s"], t["media_s"]) == (1050, 1050)
    assert t["por_decisao"]["concordo"] == {"casos": 1, "mediana_s": 900}
    assert t["por_decisao"]["discordo"] == {"casos": 1, "mediana_s": 1200}
    # sem linha de base, nenhuma economia e afirmada
    assert (t["linha_de_base"], t["economia_mediana_s"]) == (None, None)

    t = cliente.get("/metricas?linha_de_base_min=30").json()["tempo"]
    assert t["economia_mediana_s"] == 30 * 60 - 1050
    assert t["linha_de_base"]["minutos"] == 30
    assert "não medida" in t["linha_de_base"]["procedencia"]


def test_linha_de_base_invalida_e_422(cliente):
    assert cliente.get("/metricas?linha_de_base_min=0").status_code == 422
    assert cliente.get("/metricas?linha_de_base_min=-5").status_code == 422


def test_metricas_de_execucao_inexistente_e_404(cliente):
    assert cliente.get("/metricas?execucao_id=999").status_code == 404


# ============================================================================
# Fase 5.2 - o caso mostra a base como era
# ============================================================================


def _op_real(op_id):
    import json as _json
    ops = _json.loads(DADOS_REAIS.read_text(encoding="utf-8"))["operacoes"]
    return dict(next(o for o in ops if o["id"] == op_id))


def _ingerir_lote(tmp_path, operacoes, nome="lote2.json"):
    import json as _json
    arquivo = tmp_path / nome
    arquivo.write_text(_json.dumps({"taxa_cambio_usd_brl": 5.4, "operacoes": operacoes}),
                       encoding="utf-8")
    conn = db.conectar(db.CAMINHO_PADRAO)
    r = ingerir(conn, arquivo)
    conn.close()
    return r


def test_caso_antigo_mostra_as_operacoes_como_eram(cliente, tmp_path):
    """<<< aceite do 5.2 >>> Um lote novo corrige a OP-00269 (a que o parecer
    do CLI-028 cita errado) e traz uma operacao nova. O caso ANTIGO continua
    mostrando o valor que o analista viu e nao ganha a operacao que chegou
    depois; o caso da execucao nova mostra a base nova."""
    antigo = _alerta_de(cliente, "CLI-028")
    corrigida = {**_op_real("OP-00269"), "valor": 7000.0}
    nova = {**_op_real("OP-00269"), "id": "OP-90001", "valor": 1234.0}
    r = _ingerir_lote(tmp_path, [corrigida, nova])
    assert (r.operacoes_corrigidas, r.operacoes_inseridas) == (1, 1)

    def ops(alerta_id):
        return {o["id"]: o["valor_brl"] for o in cliente.get(f"/alertas/{alerta_id}").json()["operacoes"]}

    assert ops(antigo)["OP-00269"] == pytest.approx(6913.84)
    assert "OP-90001" not in ops(antigo)

    conn = db.conectar(db.CAMINHO_PADRAO)
    regras_run.executar(conn)
    conn.close()
    atual = _alerta_de(cliente, "CLI-028")
    assert atual != antigo
    assert ops(atual)["OP-00269"] == pytest.approx(7000.0)
    assert ops(atual)["OP-90001"] == pytest.approx(1234.0)


# ============================================================================
# Fase 5.1 - delta: execucao incremental, vigencia e substituicao
# ============================================================================


def test_base_nova_reprocessa_so_o_delta_sem_apagar_o_trabalho_do_analista(cliente, tmp_path, monkeypatch):
    """<<< aceite do 5.1 >>> CLI-028 ja decidido; CLI-014 em analise com a ana.
    Chega um lote com operacao nova para os dois. Esperado:
      - uma execucao incremental com exatamente 2 alertas
      - os outros 28 casos intactos (mesmo alerta, estado, dono)
      - o CLI-014 antigo nao pode ser pego nem decidido, so devolvido
      - o CLI-028 novo mostra a decisao anterior
      - o worker chama o LLM so para os 2
      - rodar as regras de novo: nada a fazer"""
    from tests_apoio import resposta_final

    aid_028, aid_014 = _alerta_de(cliente, "CLI-028"), _alerta_de(cliente, "CLI-014")
    _mudar(cliente, aid_028, "em_analise")
    assert _decidir(cliente, aid_028, "discordo", nivel_risco="alto", motivo="OP-00269").status_code == 200
    _mudar(cliente, aid_014, "em_analise", analista="ana")
    antes = {i["cliente_id"]: (i["alerta_id"], i["estado"], i["analista_id"])
             for i in cliente.get("/fila?origem=todos&limite=200").json()["itens"]}

    _ingerir_lote(tmp_path, [
        {**_op_real("OP-00269"), "id": "OP-90001", "valor": 1500.0},
        {**_op_real("OP-00269"), "id": "OP-90002", "cliente_id": "CLI-014", "valor": 800.0},
    ])
    conn = db.conectar(db.CAMINHO_PADRAO)
    r = regras_run.executar(conn)
    conn.close()
    assert (r.escopo, r.alertas_regra + r.alertas_controle) == ("incremental", 2)

    depois = {i["cliente_id"]: (i["alerta_id"], i["estado"], i["analista_id"])
              for i in cliente.get("/fila?origem=todos&limite=200").json()["itens"]}
    assert len(depois) == 30
    intactos = {c: v for c, v in antes.items() if c not in ("CLI-028", "CLI-014")}
    assert {c: depois[c] for c in intactos} == intactos
    assert depois["CLI-028"][0] != aid_028 and depois["CLI-014"][0] != aid_014

    # o alerta antigo do CLI-014: substituido, com a ana. So pode ser devolvido.
    velho = cliente.get(f"/alertas/{aid_014}").json()
    assert velho["alerta"]["substituido_por"] == depois["CLI-014"][0]
    assert velho["transicoes_permitidas"] == ["triado"]
    r_dec = _decidir(cliente, aid_014, "escalar", analista="ana", motivo="x")
    assert r_dec.status_code == 409 and "substituído" in r_dec.json()["detail"]
    assert _mudar(cliente, aid_014, "triado", analista="ana").status_code == 200
    assert _mudar(cliente, aid_014, "em_analise", analista="ana").status_code == 409

    # o CLI-028 novo carrega a decisao tomada antes do dado novo
    novo_028 = cliente.get(f"/alertas/{depois['CLI-028'][0]}").json()
    assert novo_028["decisao"] is None
    assert novo_028["decisao_anterior"]["decisao"] == "discordo"
    assert novo_028["alerta"]["substitui_alerta_id"] == aid_028

    # o worker: LLM so para os 2 (a entrada deles mudou; nenhum outro esta 'novo')
    chamadas = {"n": 0}

    def fake(**kwargs):
        chamadas["n"] += 1
        return resposta_final()

    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", fake)
    conn = db.conectar(db.CAMINHO_PADRAO)
    t = triagem.triar(conn, pausa_s=0, verbose=False)
    assert (t.triados, t.chamadas_api, chamadas["n"]) == (2, 2, 2)
    assert regras_run.executar(conn) is None
    conn.close()

    # as metricas contam a decisao tomada no alerta que foi substituido
    assert cliente.get("/metricas").json()["decididos"] == 1


def test_verificar_ambiente_le_os_vigentes_e_nao_a_ultima_execucao(cliente, tmp_path):
    """Depois de uma execucao incremental de UM cliente sem sinalizacao, a
    "ultima execucao" tem 0 fracionamento e 0 atipicas. Os numeros da base tem
    que continuar os da entrega - lidos dos vigentes."""
    import verificar_ambiente

    _ingerir_lote(tmp_path, [{**_op_real("OP-00269"), "id": "OP-90003",
                              "cliente_id": "CLI-011", "valor": 10.0}])
    conn = db.conectar(db.CAMINHO_PADRAO)
    r = regras_run.executar(conn)
    conn.close()
    assert r.escopo == "incremental" and r.clientes_fracionamento == 0

    obtido, _ = verificar_ambiente.obter_do_store()
    esperado = verificar_ambiente.ESPERADO
    for chave in ("clientes_fracionamento", "operacoes_atipicas", "top10"):
        assert obtido[chave] == esperado[chave], chave


def test_tamanhos_tem_limite_porque_o_registro_e_para_sempre(cliente):
    """Achado da auditoria: a tela limitava o motivo a 2000, a API nao."""
    aid = _alerta_de(cliente, "CLI-014")
    assert _mudar(cliente, aid, "em_analise", analista="a" * 101).status_code == 400
    _mudar(cliente, aid, "em_analise")
    r = _decidir(cliente, aid, "escalar", motivo="x" * 2001)
    assert r.status_code == 422
    assert _decidir(cliente, aid, "escalar", motivo="x" * 2000).status_code == 200
