"""Passos 1.5, 1.6 e 1.7 - o worker de triagem.

Nenhum teste aqui fala com a API: o cliente Groq e mockado. O que se verifica e
o comportamento do worker - reaproveitamento sem chamada, retomada apos
interrupcao, e o vinculo parecer <-> evidencia <-> aderencia <-> custo.
"""
import pytest
from tests_apoio import resposta_final

import agente
from dados import aplicar_regras, montar_flags, todos_os_clientes
from mesa import db, regras_run, repositorio, triagem
from mesa.importar_cache import importar
from mesa.ingestao import ingerir
from mesa.pareceres import RepositorioPareceres

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"


@pytest.fixture
def conn(tmp_path):
    c = db.conectar(tmp_path / "mesa.db")
    ingerir(c, DADOS_REAIS)
    regras_run.executar(c)
    yield c
    c.close()


@pytest.fixture
def contador_api(monkeypatch):
    """Substitui a API e conta chamadas - a metrica que interessa no worker."""
    chamadas = {"n": 0}

    def fake_create(**kwargs):
        chamadas["n"] += 1
        return resposta_final()

    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", fake_create)
    return chamadas


# ---------- 1.7: o worker ----------


def test_triagem_processa_a_fila_inteira(conn, contador_api):
    r = triagem.triar(conn, limite=5, pausa_s=0, verbose=False)

    assert r.triados == 5
    assert contador_api["n"] == 5
    assert conn.execute(
        "SELECT COUNT(*) FROM alertas WHERE estado='triado'"
    ).fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM pareceres").fetchone()[0] == 5


def test_rodar_duas_vezes_nao_reprocessa(conn, contador_api):
    """<<< aceite do 1.7 >>> A fila esvazia; a segunda passada nao custa nada."""
    triagem.triar(conn, limite=5, pausa_s=0, verbose=False)
    segunda = triagem.triar(conn, pausa_s=0, verbose=False)

    assert segunda.chamadas_api == 0 or segunda.triados == 25
    ainda_novos = conn.execute(
        "SELECT COUNT(*) FROM alertas WHERE estado='novo'"
    ).fetchone()[0]
    terceira = triagem.triar(conn, pausa_s=0, verbose=False)
    assert ainda_novos == 0
    assert terceira.triados == 0
    assert terceira.chamadas_api == 0


def test_interrupcao_no_meio_nao_perde_nem_duplica(conn, contador_api, monkeypatch):
    """<<< aceite do 1.7 >>> Mata o worker depois de gravar o parecer e antes de
    concluir o alerta - o pior momento possivel. A retomada nao pode nem perder o
    caso nem pagar a chamada de API de novo."""
    original = triagem._concluir
    estado = {"n": 0}

    def concluir_que_morre(conn_, alerta_id):
        estado["n"] += 1
        if estado["n"] == 3:
            raise KeyboardInterrupt("kill -9 simulado")
        return original(conn_, alerta_id)

    monkeypatch.setattr(triagem, "_concluir", concluir_que_morre)
    with pytest.raises(KeyboardInterrupt):
        triagem.triar(conn, limite=5, pausa_s=0, verbose=False)

    gravados = conn.execute("SELECT COUNT(*) FROM pareceres").fetchone()[0]
    api_antes = contador_api["n"]
    assert gravados == 3  # 2 concluidos + o que morreu depois de gravar

    monkeypatch.setattr(triagem, "_concluir", original)
    # limite=1 isola o invariante: o proximo da fila e justamente o alerta
    # interrompido. (`limite` conta a fila RESTANTE, nao os mesmos 5 de antes -
    # a primeira versao deste teste supos o contrario e falhou por isso.)
    retomada = triagem.triar(conn, limite=1, pausa_s=0, verbose=False)

    assert retomada.ja_tinham_parecer == 1     # recuperado, nao regerado
    assert contador_api["n"] == api_antes      # ZERO chamada de API na retomada
    assert conn.execute("SELECT COUNT(*) FROM pareceres").fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM alertas WHERE estado='triado'"
    ).fetchone()[0] == 3


def test_worker_nao_conclui_o_caso_do_analista(conn, contador_api):
    """'triado' e o agente passou. 'concluido' e decisao humana - Fase 4."""
    triagem.triar(conn, limite=3, pausa_s=0, verbose=False)
    assert conn.execute(
        "SELECT COUNT(*) FROM alertas WHERE estado='concluido'"
    ).fetchone()[0] == 0


# ---------- 4.1: o worker na trilha de transicoes ----------


def test_worker_grava_a_transicao_na_trilha(conn, contador_api):
    triagem.triar(conn, limite=3, pausa_s=0, verbose=False)
    trilha = conn.execute(
        "SELECT estado_anterior, estado_novo, ator, ator_tipo FROM transicoes"
    ).fetchall()
    assert [tuple(t) for t in trilha] == [("novo", "triado", "sistema:triagem", "sistema")] * 3


def test_caso_pego_pelo_analista_durante_a_triagem_nao_volta_para_a_fila(conn, monkeypatch):
    """A corrida que existia: o analista pega um caso 'novo' pela API enquanto o
    agente roda para ele. O UPDATE sem condicao do worker jogava o caso para
    'triado' com o analista ainda gravado como dono."""
    primeiro = triagem.fila(conn, triagem._execucao_mais_recente(conn))[0]

    def agente_lento(**kwargs):
        # enquanto o agente "pensa", a analista pega o caso pela API
        conn.execute("UPDATE alertas SET estado='em_analise', analista_id='ana' WHERE id = ?",
                     (primeiro["id"],))
        conn.commit()
        return resposta_final()

    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", agente_lento)
    triagem.triar(conn, limite=1, pausa_s=0, verbose=False)

    depois = conn.execute("SELECT estado, analista_id FROM alertas WHERE id = ?",
                          (primeiro["id"],)).fetchone()
    assert tuple(depois) == ("em_analise", "ana")
    # o parecer foi gravado e vinculado mesmo assim - a analista o vera
    assert conn.execute("SELECT COUNT(*) FROM pareceres WHERE alerta_id = ?",
                        (primeiro["id"],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transicoes").fetchone()[0] == 0


# ---------- 5.0: o agente le do store, nao do JSON ----------


def test_agente_ve_operacao_que_so_existe_no_store(conn, contador_api):
    """<<< aceite do 5.0 >>> O bug que existia: as ferramentas liam o JSON da
    entrega. Com uma operacao a mais no store, o hash do CLI-014 nao mudava e o
    parecer importado era reaproveitado em silencio, sem o agente ver o dado.

    Agora: o CLI-014 paga UMA chamada (a entrada mudou), os outros 29 continuam
    reaproveitados, e o historico que o agente recebeu conta a operacao nova."""
    import tools

    importar(conn)
    conn.execute(
        "INSERT INTO operacoes (id, lote_id, cliente_id, data, data_valida, valor, moeda, "
        "valor_brl, canal, tipo, contraparte) VALUES ('OP-99999', 1, 'CLI-014', "
        "'2026-06-01', 1, 9000.0, 'BRL', 9000.0, 'pix', 'pagamento', 'Nova Contraparte')"
    )
    conn.commit()
    vistos = []
    original = tools.historico_cliente

    def espiar(cliente_id):
        r = original(cliente_id)
        vistos.append((cliente_id, r.get("qtd_operacoes")))
        return r

    import agente
    agente_hist = agente.historico_cliente
    agente.historico_cliente = espiar
    try:
        r = triagem.triar(conn, pausa_s=0, verbose=False)
    finally:
        agente.historico_cliente = agente_hist

    assert contador_api["n"] == 1 and r.chamadas_api == 1
    assert r.reaproveitados == 29
    assert ("CLI-014", 12) in vistos          # 11 do JSON + a do store
    # e a entrega volta a ler o JSON depois da triagem
    assert tools.historico_cliente("CLI-014")["qtd_operacoes"] == 11


# ---------- 1.5: reaproveitamento ----------


def test_cache_importado_evita_chamada_de_api(conn, contador_api):
    """<<< aceite do 1.5 >>> Com o historico importado, triar os 30 alertas nao
    deve custar 30 chamadas - e cada alerta ainda assim ganha seu parecer."""
    importar(conn)
    r = triagem.triar(conn, pausa_s=0, verbose=False)

    assert r.triados == 30
    assert r.reaproveitados > 0
    assert contador_api["n"] == 30 - r.reaproveitados
    com_parecer = conn.execute(
        "SELECT COUNT(DISTINCT alerta_id) FROM pareceres WHERE alerta_id IS NOT NULL"
    ).fetchone()[0]
    assert com_parecer == 30


def test_reaproveitado_aponta_para_a_origem(conn, contador_api):
    importar(conn)
    triagem.triar(conn, pausa_s=0, verbose=False)

    copias = conn.execute(
        "SELECT p.id, p.reaproveitado_de, o.origem_registro FROM pareceres p "
        "JOIN pareceres o ON o.id = p.reaproveitado_de"
    ).fetchall()
    assert copias, "nenhum parecer reaproveitado - o teste nao exercita nada"
    for copia in copias:
        assert copia["origem_registro"] == "importado_cache"


def test_reaproveitamento_copia_a_evidencia_junto(conn):
    """Sem a evidencia, o caso vinculado ao alerta novo nao e reabrivel."""
    repo = RepositorioPareceres(conn)
    repo.salvar("h1", {
        "cliente_id": "CLI-014", "parecer": {"nivel_risco": "alto", "tipologia_suspeita": "t",
                                             "red_flags": [], "justificativa": "j"},
        "erro_parsing": None, "texto_bruto": None, "tokens_total": 1, "latencia_s": 0.1,
        "tools_chamadas": [{"tool": "perfil_canal", "args": {"cliente_id": "CLI-014"},
                            "payload": {"canal_predominante": "ted"}}],
    })
    alerta_id = repo.alerta_atual("CLI-023")
    novo_id = repo.reaproveitar("h1", alerta_id)

    evidencias = conn.execute(
        "SELECT tool, payload_json FROM evidencias WHERE parecer_id = ?", (novo_id,)
    ).fetchall()
    assert len(evidencias) == 1
    assert evidencias[0]["tool"] == "perfil_canal"


def test_reaproveitar_hash_inexistente_devolve_none(conn):
    repo = RepositorioPareceres(conn)
    assert repo.reaproveitar("nao-existe", repo.alerta_atual("CLI-014")) is None


# ---------- 1.6: aderencia e custo ----------


def test_aderencia_e_gravada_junto_do_parecer(conn, contador_api):
    triagem.triar(conn, limite=3, pausa_s=0, verbose=False)
    sem_aderencia = conn.execute(
        "SELECT COUNT(*) FROM pareceres p "
        "LEFT JOIN aderencia a ON a.parecer_id = p.id WHERE a.parecer_id IS NULL"
    ).fetchone()[0]
    assert sem_aderencia == 0


def test_custo_por_chamada_e_gravado_e_vinculado(conn, contador_api):
    triagem.triar(conn, limite=3, pausa_s=0, verbose=False)
    linhas = conn.execute(
        "SELECT tipo_turno, tokens_entrada, tokens_saida, custo_usd, parecer_id "
        "FROM chamadas_llm"
    ).fetchall()
    assert len(linhas) == 3
    for linha in linhas:
        assert linha["tipo_turno"] == "resposta_final"
        assert linha["custo_usd"] > 0
        assert linha["parecer_id"] is not None


def test_reaproveitado_nao_gera_linha_de_custo(conn, contador_api):
    """Reaproveitar nao chama a API; registrar custo seria inventar despesa."""
    importar(conn)
    triagem.triar(conn, pausa_s=0, verbose=False)
    chamadas = conn.execute("SELECT COUNT(*) FROM chamadas_llm").fetchone()[0]
    reaproveitados = conn.execute(
        "SELECT COUNT(*) FROM pareceres WHERE reaproveitado_de IS NOT NULL"
    ).fetchone()[0]
    assert chamadas == 30 - reaproveitados


# ---------- as flags do store tem que ser as mesmas do DataFrame ----------


def test_flags_do_store_batem_com_montar_flags(conn):
    """Se divergirem, o hash muda e o worker regera 30 pareceres a peso de API."""
    df = aplicar_regras(repositorio.operacoes_df(conn))
    clientes = todos_os_clientes(df).set_index("cliente_id")
    execucao_id = triagem._execucao_mais_recente(conn)

    for alerta in triagem.fila(conn, execucao_id):
        do_store = triagem.montar_flags_do_alerta(conn, alerta)
        do_df = montar_flags(df, clientes.loc[alerta["cliente_id"]].rename(alerta["cliente_id"]).to_frame().T.reset_index().rename(columns={"index": "cliente_id"}).iloc[0])
        assert do_store == do_df, alerta["cliente_id"]
