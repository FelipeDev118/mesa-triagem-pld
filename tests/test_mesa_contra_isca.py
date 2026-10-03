"""Fase 6 - contra-isca: o cenario plantado (6.0) e o motor de ligacoes (6.1, 6.5).

O cenario e montado UMA vez por sessao (semente 7, a de desenvolvimento) e
copiado para quem escreve. As sementes de medicao (101 a 120) nao aparecem
aqui: sao a prova do 6.2, e nao podem ter servido para ajustar o motor.
"""
import json
import shutil

import pytest

from mesa import cenario_isca, contra_isca, db
from mesa.ingestao import ingerir

SEMENTE_DEV = 7


@pytest.fixture(scope="session")
def _cenario(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("cenario")
    caminho, gabarito = cenario_isca.montar(pasta, SEMENTE_DEV)
    conn = db.conectar(caminho)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return caminho, gabarito


@pytest.fixture
def cenario(_cenario, tmp_path):
    """Copia do cenario, com conexao aberta - pode escrever."""
    origem, gabarito = _cenario
    caminho = tmp_path / "mesa.db"
    shutil.copy(origem, caminho)
    conn = db.conectar(caminho)
    yield conn, gabarito
    conn.close()


def _ids(resultado, n=None):
    return [l["operacao_id"] for l in resultado["ligacoes"][:n]]


# ============================================================================
# 6.0 - o cenario, antes do detector
# ============================================================================


def test_regras_de_hoje_nao_pegam_nenhuma_operacao_plantada(cenario):
    """O ponto cego, provado: nenhuma sinalizacao aponta operacao plantada, e
    todo cliente plantado fica como 'controle' (sem sinal nenhum)."""
    conn, g = cenario
    plantadas = g["padrao_a"] + g["padrao_b"]
    marcas = ",".join("?" * len(plantadas))
    assert conn.execute(
        f"SELECT COUNT(*) FROM sinalizacoes WHERE operacao_id IN ({marcas})", plantadas
    ).fetchone()[0] == 0

    clientes = {r[0] for r in conn.execute(
        f"SELECT DISTINCT cliente_id FROM operacoes WHERE id IN ({marcas})", plantadas
    )}
    origens = {r[0] for r in conn.execute(
        f"SELECT a.origem FROM alertas a WHERE a.cliente_id IN ({','.join('?' * len(clientes))})",
        sorted(clientes),
    )}
    assert origens == {"controle"}


def test_a_isca_e_chamativa_a_regra_pega(cenario):
    conn, g = cenario
    alerta = conn.execute("SELECT origem, cliente_id FROM alertas WHERE id = ?",
                          (g["alerta_isca"],)).fetchone()
    assert alerta["origem"] == "regra" and alerta["cliente_id"] == cenario_isca.ISCA


def test_gabarito_gravado_junto_do_cenario(_cenario):
    caminho, g = _cenario
    gravado = json.loads((caminho.parent / "gabarito.json").read_text(encoding="utf-8"))
    assert gravado == g
    assert 5 <= len(g["padrao_a"]) <= 8 and len(g["padrao_b"]) == 3
    for nome in ("lote_cenario", "lote_durante_analise", "lote_depois_da_analise"):
        assert (caminho.parent / f"{nome}.json").exists()


def test_cenario_e_deterministico_pela_semente():
    a, b, c = cenario_isca.gerar(3), cenario_isca.gerar(3), cenario_isca.gerar(4)
    assert a.lotes == b.lotes and a.gabarito == b.gabarito
    assert a.lotes != c.lotes


def test_cenario_nao_escreve_por_cima_de_banco(tmp_path):
    (tmp_path / "mesa.db").write_bytes(b"")
    with pytest.raises(FileExistsError):
        cenario_isca.montar(tmp_path, 1)


def test_horarios_fabricados_poem_o_padrao_b_dentro_da_analise(cenario):
    conn, _ = cenario
    lotes = [r[0] for r in conn.execute("SELECT ingerido_em FROM lotes_ingestao ORDER BY id")]
    trilha = [r[0] for r in conn.execute("SELECT registrado_em FROM transicoes ORDER BY id")]
    h = cenario_isca.HORARIOS
    assert lotes == [h["lote_base"], h["lote_cenario"], h["lote_durante_analise"],
                     h["lote_depois_da_analise"]]
    assert trilha == [h["isca_pega"], h["isca_devolvida"]]


# ============================================================================
# 6.1 - o motor, padrao A
# ============================================================================


def test_plantadas_do_padrao_a_no_topo(cenario):
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    assert set(_ids(r, len(g["padrao_a"]))) == set(g["padrao_a"])


def test_contraparte_popular_nao_sobe(cenario):
    """O ruido (1) esta na janela e na faixa abaixo do limite - parece
    fracionamento. So o peso da contraparte o separa, e ele tem que bastar."""
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    escore = {l["operacao_id"]: l["escore"] for l in r["ligacoes"]}
    pior_plantada = min(escore[o] for o in g["padrao_a"] + g["padrao_b"])
    for o in g["ruido"]["popular_na_janela"]:
        assert escore[o] < pior_plantada / 2


def test_fora_da_janela_nao_e_ligacao(cenario):
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    assert not set(g["ruido"]["fora_da_janela"]) & set(_ids(r))


def test_faixa_sai_dos_parametros_gravados_na_execucao(cenario):
    """Baixar o limite individual GRAVADO na execucao do alerta muda quem esta
    "logo abaixo do limite" - o motor nao tem o numero fixo."""
    conn, g = cenario
    antes = contra_isca.cacar(conn, g["alerta_isca"])
    acima_de_19k = [l["operacao_id"] for l in antes["ligacoes"]
                    if l["operacao_id"] in g["padrao_a"] and l["valor_brl"] >= 19_000]
    assert acima_de_19k, "semente sem plantada >= 19 mil - o teste nao exercitaria nada"

    execucao = conn.execute("SELECT execucao_id FROM alertas WHERE id = ?",
                            (g["alerta_isca"],)).fetchone()[0]
    params = json.loads(conn.execute("SELECT parametros_json FROM execucoes_regras WHERE id = ?",
                                     (execucao,)).fetchone()[0])
    params["frac_max_individual"] = 19_000
    conn.execute("UPDATE execucoes_regras SET parametros_json = ? WHERE id = ?",
                 (json.dumps(params), execucao))

    depois = {l["operacao_id"]: l for l in contra_isca.cacar(conn, g["alerta_isca"])["ligacoes"]}
    for o in acima_de_19k:
        assert "faixa" not in {c["nome"] for c in depois[o]["componentes"]}
    faixas = [c["procedencia"] for l in depois.values() for c in l["componentes"] if c["nome"] == "faixa"]
    assert faixas and all("R$ 19.000,00" in f for f in faixas)


def test_toda_ligacao_diz_de_onde_vem_cada_numero(cenario):
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    assert r["ligacoes"]
    for l in r["ligacoes"]:
        comps = l["componentes"]
        assert comps[0]["nome"] == "contraparte"
        assert all(c["procedencia"].strip() for c in comps)
        # escore = peso da contraparte x soma dos demais: reproduzivel da tela
        assert l["escore"] == pytest.approx(comps[0]["valor"] * sum(c["valor"] for c in comps[1:]),
                                            abs=1e-3)


def test_plantada_traz_as_frases_que_a_explicam(cenario):
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    topo = r["ligacoes"][0]
    nomes = {c["nome"] for c in topo["componentes"]}
    assert {"contraparte", "janela", "faixa", "distribuicao"} <= nomes
    frases = " ".join(c["procedencia"] for c in topo["componentes"])
    assert g["contrapartes"]["padrao_a"] in frases
    assert "nenhum outro cliente a usa" in frases
    assert "do limite individual de R$ 20.000,00" in frases


def test_coincidencia_na_janela_sem_faixa_nao_conta_como_distribuicao(cenario, tmp_path):
    """3 clientes usando a mesma contraparte da isca na janela, so UM deles
    logo abaixo do limite: e coincidencia de base, nao fracionamento
    distribuido. Contar qualquer ligacao como distribuicao dava ao da faixa
    "liga 3 clientes" (achado na semente 7)."""
    conn, g = cenario
    dia = g["dia_isca"]
    valores = [2_000.0, 19_000.0, 2_100.0, 2_200.0]   # so o CLI-191 na faixa
    ops = [{"id": f"OPX-COINC-{i}", "cliente_id": c, "data": dia, "valor": valores[i],
            "moeda": "BRL", "canal": "pix", "tipo": "pagamento",
            "contraparte": "Coincidencia Teste SA", "observacao": ""}
           for i, c in enumerate([cenario_isca.ISCA, "CLI-191", "CLI-192", "CLI-193"])]
    arquivo = tmp_path / "coinc.json"
    arquivo.write_text(json.dumps({"taxa_cambio_usd_brl": 5.4, "operacoes": ops}), encoding="utf-8")
    ingerir(conn, arquivo)

    por_id = {l["operacao_id"]: l for l in contra_isca.cacar(conn, g["alerta_isca"])["ligacoes"]}
    for i in (1, 2, 3):
        assert "distribuicao" not in {c["nome"] for c in por_id[f"OPX-COINC-{i}"]["componentes"]}


def test_cacar_nao_escreve_nada(cenario):
    conn, g = cenario
    antes = conn.total_changes
    contra_isca.cacar(conn, g["alerta_isca"])
    assert conn.total_changes == antes


def test_alerta_inexistente(cenario):
    conn, _ = cenario
    with pytest.raises(LookupError):
        contra_isca.cacar(conn, 99_999)


def test_mesma_consulta_mesma_ordem(cenario):
    conn, g = cenario
    assert contra_isca.cacar(conn, g["alerta_isca"]) == contra_isca.cacar(conn, g["alerta_isca"])


# ============================================================================
# 6.5 - padrao B: o que entrou enquanto o caso estava em analise
# ============================================================================


def test_padrao_b_sobe_sem_estar_abaixo_de_limite_nenhum(cenario):
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    topo = set(_ids(r, len(g["padrao_a"]) + len(g["padrao_b"])))
    por_id = {l["operacao_id"]: l for l in r["ligacoes"]}
    for o in g["padrao_b"]:
        assert o in topo
        assert "B" in por_id[o]["padroes"]
        assert por_id[o]["valor_brl"] > 20_000          # bem acima do limite individual
        assert "faixa" not in {c["nome"] for c in por_id[o]["componentes"]}


def test_lote_depois_da_analise_nao_e_ligacao(cenario):
    """Mesma contraparte do B, mesmo valor alto, mesma data - so o horario do
    lote muda (chegou depois de o caso ser devolvido). Fica de fora."""
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    assert not set(g["ruido"]["lote_depois_da_analise"]) & set(_ids(r))


def test_periodo_vem_da_trilha(cenario):
    conn, g = cenario
    r = contra_isca.cacar(conn, g["alerta_isca"])
    h = cenario_isca.HORARIOS
    assert r["periodos_em_analise"] == [
        {"inicio": h["isca_pega"], "fim": h["isca_devolvida"], "analista": cenario_isca.ANALISTA_CENARIO}
    ]


def test_caso_ainda_em_analise_pega_o_que_chega_agora(cenario, tmp_path):
    """Periodo ABERTO: pegar a isca de novo e ingerir um lote - o que esta
    passando agora entra, sem precisar o caso sair de analise."""
    conn, g = cenario
    isca = g["alerta_isca"]
    conn.execute("UPDATE alertas SET estado = 'em_analise', analista_id = 'ana' WHERE id = ?", (isca,))
    conn.execute(
        "INSERT INTO transicoes (alerta_id, estado_anterior, estado_novo, ator, ator_tipo, registrado_em) "
        "VALUES (?, 'triado', 'em_analise', 'ana', 'analista', '2026-06-03T09:00:00Z')", (isca,))
    arquivo = tmp_path / "agora.json"
    arquivo.write_text(json.dumps({"taxa_cambio_usd_brl": 5.4, "operacoes": [{
        "id": "OPX-AGORA", "cliente_id": "CLI-199", "data": "2026-06-03", "valor": 95_000.0,
        "moeda": "BRL", "canal": "ted", "tipo": "transferencia_recebida",
        "contraparte": g["contrapartes"]["padrao_b"], "observacao": ""}]}), encoding="utf-8")
    lote = ingerir(conn, arquivo).lote_id
    conn.execute("UPDATE lotes_ingestao SET ingerido_em = '2026-06-03T09:30:00Z' WHERE id = ?", (lote,))

    r = contra_isca.cacar(conn, isca)
    agora = next(l for l in r["ligacoes"] if l["operacao_id"] == "OPX-AGORA")
    assert agora["padroes"] == ["B"]
    assert "→ agora" in next(c["procedencia"] for c in agora["componentes"] if c["nome"] == "analise")


def test_operacao_antiga_que_chega_durante_a_analise_nao_e_padrao_b(cenario, tmp_path):
    """Achado no cenario: o historico de um cliente novo chegava inteiro no
    lote da analise. Uma operacao de ABRIL ingerida em junho nao passou
    "enquanto olhavamos" - nao e B (e, longe da janela de dias, nao e ligacao)."""
    conn, g = cenario
    isca = g["alerta_isca"]
    arquivo = tmp_path / "antiga.json"
    arquivo.write_text(json.dumps({"taxa_cambio_usd_brl": 5.4, "operacoes": [{
        "id": "OPX-ANTIGA", "cliente_id": "CLI-198", "data": "2026-03-02", "valor": 95_000.0,
        "moeda": "BRL", "canal": "ted", "tipo": "transferencia_recebida",
        "contraparte": g["contrapartes"]["padrao_b"], "observacao": ""}]}), encoding="utf-8")
    lote = ingerir(conn, arquivo).lote_id
    h = cenario_isca.HORARIOS
    conn.execute("UPDATE lotes_ingestao SET ingerido_em = ? WHERE id = ?",
                 ("2026-06-02T10:30:00Z", lote))  # dentro do periodo pega -> devolvida
    assert h["isca_pega"] < "2026-06-02T10:30:00Z" < h["isca_devolvida"]
    assert "OPX-ANTIGA" not in _ids(contra_isca.cacar(conn, isca))


def test_periodos_em_analise_da_trilha():
    t = lambda de, para, quando, ator="ana": {"estado_anterior": de, "estado_novo": para,
                                               "registrado_em": quando, "ator": ator}
    # saida sem entrada (trilha anterior ao esquema v6) nao inventa periodo
    assert contra_isca.periodos_em_analise([t("em_analise", "triado", "T1")]) == []
    assert contra_isca.periodos_em_analise([
        t("novo", "triado", "T0", "sistema:triagem"),
        t("triado", "em_analise", "T1"), t("em_analise", "triado", "T2"),
        t("triado", "em_analise", "T3", "bia"),
    ]) == [("T1", "T2", "ana"), ("T3", None, "bia")]


# ============================================================================
# 6.3 - esquema v8 e API
# ============================================================================

import sqlite3  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from mesa import api  # noqa: E402


@pytest.fixture
def cliente(_cenario, tmp_path, monkeypatch):
    origem, gabarito = _cenario
    caminho = tmp_path / "api.db"
    shutil.copy(origem, caminho)
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)
    return TestClient(api.app), gabarito, caminho


def _uma_plantada(g):
    return g["padrao_a"][0]


def _cliente_da(c, g, op):
    caca = c.get(f"/alertas/{g['alerta_isca']}/contra-isca").json()
    return next(l["cliente_id"] for l in caca["ligacoes"] if l["operacao_id"] == op)


def test_api_devolve_a_caca_com_procedencia(cliente):
    c, g, _ = cliente
    r = c.get(f"/alertas/{g['alerta_isca']}/contra-isca")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["cliente_id"] == cenario_isca.ISCA
    assert corpo["limites"]["frac_max_individual"] == 20_000
    assert corpo["versao_caca"] == contra_isca.VERSAO_CACA
    topo = {l["operacao_id"] for l in corpo["ligacoes"][:len(g["padrao_a"]) + len(g["padrao_b"])]}
    assert topo == set(g["padrao_a"]) | set(g["padrao_b"])
    assert corpo["suspeitas"] == []


def test_api_caca_de_alerta_inexistente_e_404(cliente):
    c, _, _ = cliente
    assert c.get("/alertas/99999/contra-isca").status_code == 404


def test_caca_pela_api_nao_escreve(cliente):
    c, g, caminho = cliente
    antes = caminho.read_bytes()
    c.get(f"/alertas/{g['alerta_isca']}/contra-isca")
    assert caminho.read_bytes() == antes


def test_registrar_suspeita_grava_o_retrato_do_servidor(cliente):
    c, g, _ = cliente
    op = _uma_plantada(g)
    alvo = _cliente_da(c, g, op)
    r = c.post(f"/alertas/{g['alerta_isca']}/suspeitas", headers={"X-Analista": "ana"},
               json={"cliente_id": alvo, "operacoes": [op], "motivo": "  mesma contraparte rara  "})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["analista_id"] == "ana" and s["motivo"] == "mesma contraparte rara"
    assert s["cliente_origem"] == cenario_isca.ISCA and s["cliente_id"] == alvo
    assert s["ligacoes"][0]["operacao_id"] == op and s["ligacoes"][0]["componentes"]
    assert s["versao_caca"] == contra_isca.VERSAO_CACA

    # aparece no caso de origem, no caso do cliente apontado e na propria caca
    assert [x["suspeita_id"] for x in c.get(f"/alertas/{g['alerta_isca']}").json()
            ["suspeitas_registradas"]] == [s["suspeita_id"]]
    caca = c.get(f"/alertas/{g['alerta_isca']}/contra-isca").json()
    assert [x["suspeita_id"] for x in caca["suspeitas"]] == [s["suspeita_id"]]


def test_suspeita_aparece_no_caso_do_cliente_apontado(cliente):
    c, g, caminho = cliente
    op = _uma_plantada(g)
    alvo = _cliente_da(c, g, op)
    c.post(f"/alertas/{g['alerta_isca']}/suspeitas", headers={"X-Analista": "ana"},
           json={"cliente_id": alvo, "operacoes": [op], "motivo": "fracionamento distribuido"})
    conn = sqlite3.connect(caminho)
    alerta_alvo = conn.execute(
        "SELECT a.id FROM alertas a WHERE a.cliente_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM alertas s WHERE s.substitui_alerta_id = a.id)", (alvo,)).fetchone()[0]
    conn.close()
    sobre = c.get(f"/alertas/{alerta_alvo}").json()["suspeitas_sobre_o_cliente"]
    assert len(sobre) == 1 and sobre[0]["cliente_origem"] == cenario_isca.ISCA


def test_suspeita_exige_analista_e_motivo(cliente):
    c, g, _ = cliente
    op = _uma_plantada(g)
    alvo = _cliente_da(c, g, op)
    url = f"/alertas/{g['alerta_isca']}/suspeitas"
    corpo = {"cliente_id": alvo, "operacoes": [op], "motivo": "x"}
    assert c.post(url, json=corpo).status_code == 400
    assert c.post(url, headers={"X-Analista": "ana"}, json={**corpo, "motivo": "   "}).status_code == 422
    assert c.post(url, headers={"X-Analista": "ana"}, json={**corpo, "operacoes": []}).status_code == 422
    assert c.post(url, headers={"X-Analista": "ana"},
                  json={**corpo, "motivo": "m" * (api.MAX_MOTIVO + 1)}).status_code == 422


def test_suspeita_de_operacao_que_nao_e_ligacao_e_recusada(cliente):
    """A operacao tem que ser ligacao ATUAL daquele cliente: nem operacao de
    outro cliente, nem o ruido que ficou fora da janela."""
    c, g, _ = cliente
    url = f"/alertas/{g['alerta_isca']}/suspeitas"
    op = _uma_plantada(g)
    alvo = _cliente_da(c, g, op)
    outra = next(o for o in g["padrao_a"] if _cliente_da(c, g, o) != alvo)
    for ops in ([outra], [op, g["ruido"]["fora_da_janela"][0]]):
        r = c.post(url, headers={"X-Analista": "ana"},
                   json={"cliente_id": alvo, "operacoes": ops, "motivo": "teste"})
        assert r.status_code == 409, ops
    assert c.get(f"/alertas/{g['alerta_isca']}").json()["suspeitas_registradas"] == []


def test_suspeita_repetida_e_recusada(cliente):
    c, g, _ = cliente
    op = _uma_plantada(g)
    corpo = {"cliente_id": _cliente_da(c, g, op), "operacoes": [op], "motivo": "duplo clique"}
    url = f"/alertas/{g['alerta_isca']}/suspeitas"
    assert c.post(url, headers={"X-Analista": "ana"}, json=corpo).status_code == 201
    r = c.post(url, headers={"X-Analista": "bia"}, json=corpo)
    assert r.status_code == 409 and "ana" in r.json()["detail"]


def test_suspeitas_sao_append_only_no_schema(cliente):
    c, g, caminho = cliente
    op = _uma_plantada(g)
    c.post(f"/alertas/{g['alerta_isca']}/suspeitas", headers={"X-Analista": "ana"},
           json={"cliente_id": _cliente_da(c, g, op), "operacoes": [op], "motivo": "m"})
    conn = db.conectar(caminho)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE suspeitas SET motivo = 'outro'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM suspeitas")
    conn.close()


@pytest.mark.parametrize("motivo,analista", [(None, "ana"), ("  ", "ana"), ("m", "  ")])
def test_suspeita_sem_motivo_ou_analista_nao_entra_nem_direto_no_banco(tmp_path, motivo, analista):
    conn = db.conectar(tmp_path / "x.db")
    conn.execute("INSERT INTO lotes_ingestao (id, origem, sha256_arquivo, taxa_cambio_usd_brl, "
                 "operacoes_brutas, operacoes_inseridas, duplicatas_ignoradas, datas_nulas_brutas, "
                 "operacoes_usd_brutas, ingerido_em) VALUES (1,'x','x',5.4,0,0,0,0,0,'t')")
    conn.execute("INSERT INTO execucoes_regras (id, lote_id, versao_regras, parametros_json, "
                 "executado_em, operacoes_avaliadas, clientes_fracionamento, operacoes_atipicas) "
                 "VALUES (1,1,'r','{}','t',0,0,0)")
    conn.execute("INSERT INTO alertas (id, execucao_id, cliente_id, origem, sinalizacoes_fracionamento, "
                 "sinalizacoes_valor_atipico, total_sinalizacoes, volume_total_brl, qtd_operacoes, "
                 "nivel_risco_regra, criado_em) VALUES (1,1,'C','regra',0,1,1,1,1,'médio','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO suspeitas (alerta_origem_id, cliente_id, operacoes_json, "
                     "ligacoes_json, versao_caca, analista_id, motivo, registrado_em) "
                     "VALUES (1, 'D', '[]', '[]', 'c1', ?, ?, 't')", (analista, motivo))
    conn.close()


def test_medicao_da_semente_de_desenvolvimento(tmp_path):
    m = cenario_isca.medir_semente(tmp_path, SEMENTE_DEV)
    assert m["cobertura_a"] == m["cobertura_b"] == m["precisao_topo"] == 1.0
    assert m["popular_no_topo"] == m["fora_da_janela_na_lista"] == m["depois_da_analise_na_lista"] == 0
