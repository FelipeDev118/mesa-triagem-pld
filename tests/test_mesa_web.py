"""Fase 3 - a tela: servida pela API, sem dependencia externa, sem innerHTML.

Os dois testes de "regra da tela" existem para que as decisoes da Fase 3 nao
dependam de alguem lembrar delas:
  - nenhum recurso externo: a ferramenta tem que abrir numa rede de banco sem
    internet (e nenhum dado de caso vaza para um CDN via Referer)
  - nenhum innerHTML: a justificativa e texto gerado por LLM; inserida como
    HTML, uma saida malformada ou maliciosa viraria codigo no navegador do
    analista

A logica pura (logica.js) e testada pelo test runner do Node; o ultimo teste
daqui o executa, para a suite inteira ter um ponto de entrada so.
"""
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from mesa import api

WEB = api.WEB_DIR
ARQUIVOS_DA_TELA = ["index.html", "app.js", "logica.js", "estilo.css"]


@pytest.fixture
def cliente():
    # A tela e estatica: nao precisa de store para ser servida
    return TestClient(api.app)


# ---------- 3.0: a API serve a tela ----------


def test_raiz_redireciona_para_a_tela(cliente):
    r = cliente.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/app/"


def test_tela_e_servida_em_app(cliente):
    """<<< aceite do 3.0 >>>"""
    r = cliente.get("/app/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>Mesa de Triagem PLD</title>" in r.text


@pytest.mark.parametrize("arquivo,tipo", [
    ("app.js", "javascript"),
    ("logica.js", "javascript"),
    ("estilo.css", "text/css"),
])
def test_arquivos_da_tela_saem_com_o_tipo_certo(cliente, arquivo, tipo):
    """Um modulo ES servido com tipo errado e recusado pelo navegador em
    silencio - a tela abriria em branco sem erro visivel no servidor."""
    r = cliente.get(f"/app/{arquivo}")
    assert r.status_code == 200
    assert tipo in r.headers["content-type"]


@pytest.mark.parametrize("arquivo", ["", "app.js", "logica.js", "estilo.css"])
def test_arquivos_da_tela_obrigam_o_navegador_a_revalidar(cliente, arquivo):
    """O bug real da Fase 4: apos atualizar o codigo, o navegador reusou o
    app.js antigo sem perguntar ao servidor (o log mostrava so o GET do
    index.html) e o botao Metricas nao fazia nada."""
    r = cliente.get(f"/app/{arquivo}")
    assert r.headers["cache-control"] == "no-cache"
    # e a revalidacao continua barata: sem mudanca, 304
    etag = r.headers["etag"]
    assert cliente.get(f"/app/{arquivo}", headers={"If-None-Match": etag}).status_code == 304


def test_documentacao_da_api_continua_de_pe(cliente):
    """<<< aceite do 3.0 >>> Montar a tela nao pode sombrear as rotas da API."""
    assert cliente.get("/docs").status_code == 200
    rotas = set(cliente.get("/openapi.json").json()["paths"])
    assert {"/saude", "/fila", "/alertas/{alerta_id}", "/execucoes"} <= rotas


def test_arquivo_inexistente_da_tela_e_404(cliente):
    assert cliente.get("/app/nao-existe.js").status_code == 404


# ---------- regras da tela ----------


@pytest.mark.parametrize("arquivo", ARQUIVOS_DA_TELA)
def test_tela_nao_carrega_nada_de_fora(arquivo):
    """Nenhum http(s):// em lugar nenhum da tela: nem fonte, nem CDN, nem script.
    Os comentarios contam tambem - uma URL comentada hoje e um <link> amanha."""
    conteudo = (WEB / arquivo).read_text(encoding="utf-8")
    externas = re.findall(r"https?://[^\s\"')]+", conteudo)
    # Unica excecao, e nomeada: o namespace do SVG no icone embutido. E um
    # identificador que o SVG exige, nao um endereco - o navegador nunca o busca.
    externas = [u for u in externas if u != "http://www.w3.org/2000/svg"]
    assert externas == [], externas


def test_nenhum_dado_entra_na_pagina_como_html():
    """O texto do parecer e gerado por LLM. Todo conteudo vindo da API entra como
    no de texto (funcao h() em app.js) - nunca por innerHTML/outerHTML/
    insertAdjacentHTML/document.write."""
    for arquivo in ["app.js", "logica.js"]:
        codigo = (WEB / arquivo).read_text(encoding="utf-8")
        # ignora as linhas de comentario que EXPLICAM a regra
        sem_comentarios = "\n".join(
            l for l in codigo.splitlines() if not l.lstrip().startswith("//")
        )
        for perigoso in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"]:
            assert perigoso not in sem_comentarios, f"{perigoso} em {arquivo}"


def test_logica_nao_procura_numero_no_texto():
    """A tela NAO reimplementa o parser do verificador: as posicoes vem gravadas
    (aderencia.marcas). Um regex de moeda aqui seria a segunda copia de um
    parser que ja teve cinco bugs de formato em Python."""
    codigo = (WEB / "logica.js").read_text(encoding="utf-8") + (WEB / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"R\\\$", codigo), "regex de R$ na tela - use as marcas da API"


# ---------- a logica pura, pelo Node ----------


@pytest.mark.skipif(shutil.which("node") is None, reason="node nao instalado nesta maquina")
def test_logica_da_tela_no_node():
    r = subprocess.run(
        ["node", "--test", "tests/web/*.test.mjs"],
        cwd=api.db.RAIZ, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert re.search(r"# fail 0", r.stdout), r.stdout[-1500:]


def test_enderecos_dos_arquivos_mudam_quando_o_conteudo_muda(cliente, tmp_path, monkeypatch):
    """O no-cache nao bastou na pratica: o navegador reusou copias guardadas
    ANTES do cabecalho existir, sem perguntar (o log so tinha o GET do index).
    O index passa a apontar para app.js?v=<hash do conteudo> - endereco novo,
    arquivo novo. Inclusive para o logica.js importado de DENTRO do app.js
    (import map): versionar so o app.js deixaria a tela nova com a logica velha."""
    import re
    import shutil

    from mesa import api

    html = cliente.get("/app/").text
    assert "{{versao}}" not in html
    versao = re.search(r'app\.js\?v=([0-9a-f]{12})', html).group(1)
    assert f'estilo.css?v={versao}' in html
    assert f'"./logica.js": "./logica.js?v={versao}"' in html

    copia = tmp_path / "web"
    shutil.copytree(api.WEB_DIR, copia)
    monkeypatch.setattr(api, "WEB_DIR", copia)
    assert re.search(r'app\.js\?v=([0-9a-f]{12})', cliente.get("/app/").text).group(1) == versao
    (copia / "logica.js").write_text((copia / "logica.js").read_text() + "\n// mudou\n")
    nova = re.search(r'app\.js\?v=([0-9a-f]{12})', cliente.get("/app/").text).group(1)
    assert nova != versao
