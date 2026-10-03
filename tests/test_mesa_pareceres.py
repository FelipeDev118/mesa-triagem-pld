"""Passo 1.1 - tabelas da Fase 1 e a imutabilidade do parecer.

O teste que importa aqui e o dos triggers: "parecer nunca se sobrescreve" tem
que ser garantia do banco, nao disciplina de quem escreve o INSERT.
"""
import json
import sqlite3

import pytest

from mesa import db

CAMPOS = (
    "cliente_id, hash_entrada, nivel_risco, modelo, versao_prompt, criado_em"
)
VALORES = "('CLI-014', 'abc123', 'alto', 'gpt-oss', 'v3', '2026-09-12T00:00:00Z')"


@pytest.fixture
def conn(tmp_path):
    c = db.conectar(tmp_path / "mesa.db")
    yield c
    c.close()


def test_tabelas_da_fase_1_existem(conn):
    tabelas = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"pareceres", "evidencias", "aderencia", "chamadas_llm"} <= tabelas


def test_parecer_nao_pode_ser_atualizado(conn):
    conn.execute(f"INSERT INTO pareceres ({CAMPOS}) VALUES {VALORES}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE pareceres SET nivel_risco = 'baixo' WHERE id = 1")


def test_parecer_nao_pode_ser_apagado(conn):
    conn.execute(f"INSERT INTO pareceres ({CAMPOS}) VALUES {VALORES}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM pareceres WHERE id = 1")


def test_mesmo_hash_pode_ter_varias_versoes(conn):
    """O oposto de um cache: reprocessar nao sobrescreve, empilha."""
    for i, risco in enumerate(["alto", "medio"]):
        conn.execute(
            "INSERT INTO pareceres (cliente_id, hash_entrada, nivel_risco, modelo, "
            "versao_prompt, criado_em) VALUES ('CLI-014', 'abc123', ?, 'gpt-oss', 'v3', ?)",
            (risco, f"2026-09-1{i + 2}T00:00:00Z"),
        )
    linhas = conn.execute(
        "SELECT nivel_risco FROM pareceres WHERE hash_entrada='abc123' "
        "ORDER BY criado_em DESC"
    ).fetchall()
    assert [r[0] for r in linhas] == ["medio", "alto"]  # o atual e o mais recente


def test_parecer_sem_alerta_e_permitido(conn):
    """Rodar o agente fora da fila (o python nivel_2/agente.py da entrega) produz
    parecer legitimo sem alerta. Exigir o vinculo faria a entrega parar de rodar."""
    conn.execute(f"INSERT INTO pareceres ({CAMPOS}) VALUES {VALORES}")
    assert conn.execute("SELECT alerta_id FROM pareceres").fetchone()[0] is None


def test_evidencia_exige_parecer_existente(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evidencias (parecer_id, ordem, tool, args_json, payload_json) "
            "VALUES (999, 0, 'historico_cliente', '{}', '{}')"
        )


# ---------- 1.2: drop-in de CacheParecer ----------

import agente  # noqa: E402
from cache_parecer import CacheParecer, calcular_hash  # noqa: E402
from mesa.pareceres import RepositorioPareceres  # noqa: E402
from tests_apoio import FLAGS_BASE, resposta_final, resposta_com_tool_call  # noqa: E402

CLIENTE = "CLI-014"


@pytest.fixture
def repo(conn):
    return RepositorioPareceres(conn)


def test_repositorio_e_drop_in_do_cache(monkeypatch, repo):
    """O agente nao pode notar a diferenca: mesmo comportamento de cache hit,
    mesma chave, e uma unica chamada de API para duas execucoes."""
    chamadas = {"n": 0}

    def fake_create(**kwargs):
        chamadas["n"] += 1
        return resposta_final()

    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", fake_create)

    primeiro = agente.rodar_agente(CLIENTE, FLAGS_BASE, cache=repo)
    segundo = agente.rodar_agente(CLIENTE, FLAGS_BASE, cache=repo)

    assert primeiro["cache_hit"] is False
    assert segundo["cache_hit"] is True
    assert segundo["parecer"] == primeiro["parecer"]
    assert chamadas["n"] == 1
    assert len(repo) == 1


def test_repositorio_devolve_os_mesmos_campos_que_o_cache(monkeypatch, repo, tmp_path):
    """Comparacao lado a lado com a implementacao antiga: se algum campo sumisse
    na travessia, quem consome o resultado quebraria em silencio."""
    monkeypatch.setattr(
        agente.CLIENT.chat.completions, "create",
        lambda **kw: resposta_final(),
    )
    cache_antigo = CacheParecer(caminho=tmp_path / "cache.json")

    agente.rodar_agente(CLIENTE, FLAGS_BASE, cache=cache_antigo)
    agente.rodar_agente(CLIENTE, FLAGS_BASE, cache=repo)

    hash_entrada = calcular_hash(
        CLIENTE, FLAGS_BASE, agente.historico_cliente(CLIENTE), agente.MODEL
    )
    do_cache = cache_antigo.obter(hash_entrada)
    do_repo = repo.obter(hash_entrada)

    ignorar = {"parecer_id"}  # so existe no repositorio, por natureza
    assert set(do_cache) <= set(do_repo)
    for campo in set(do_cache) - ignorar:
        assert do_repo[campo] == do_cache[campo], campo


def test_parecer_com_erro_de_parsing_tambem_e_registrado(repo):
    """Nao gravar a falha esconderia justamente o caso que precisa de analise."""
    repo.salvar("hash-erro", {
        "cliente_id": CLIENTE,
        "parecer": None,
        "erro_parsing": "numero maximo de turnos de tool-calling excedido",
        "texto_bruto": None,
        "tools_chamadas": [],
        "tokens_total": 900,
        "latencia_s": 4.1,
    })
    recuperado = repo.obter("hash-erro")
    assert recuperado["parecer"] is None
    assert "turnos" in recuperado["erro_parsing"]


def test_salvar_duas_vezes_o_mesmo_hash_empilha_e_devolve_o_mais_recente(repo):
    base = {"cliente_id": CLIENTE, "erro_parsing": None, "texto_bruto": None,
            "tools_chamadas": [], "tokens_total": 10, "latencia_s": 1.0}
    repo.salvar("h", {**base, "parecer": {"nivel_risco": "alto", "tipologia_suspeita": "a",
                                          "red_flags": [], "justificativa": "j1"}})
    repo.salvar("h", {**base, "parecer": {"nivel_risco": "baixo", "tipologia_suspeita": "b",
                                          "red_flags": [], "justificativa": "j2"}})

    assert repo.obter("h")["parecer"]["nivel_risco"] == "baixo"
    assert repo.conn.execute("SELECT COUNT(*) FROM pareceres").fetchone()[0] == 2
    assert len(repo) == 1  # um caso coberto, duas versoes dele


# ---------- 1.4: evidencia com payload ----------


def test_evidencia_guarda_o_payload_da_ferramenta(monkeypatch, repo):
    """<<< aceite do 1.4 >>> A chamada registrada tem que trazer o RETORNO, nao
    so o nome e os argumentos."""
    respostas = iter([resposta_com_tool_call(), resposta_final()])
    monkeypatch.setattr(
        agente.CLIENT.chat.completions, "create", lambda **kw: next(respostas)
    )

    agente.rodar_agente(CLIENTE, FLAGS_BASE, cache=repo)

    linhas = repo.conn.execute(
        "SELECT tool, args_json, payload_json FROM evidencias ORDER BY ordem"
    ).fetchall()
    assert len(linhas) == 1
    assert linhas[0]["tool"] == "historico_cliente"
    payload = json.loads(linhas[0]["payload_json"])
    assert payload["cliente_id"] == CLIENTE
    assert payload["qtd_operacoes"] == 11  # dado real da base, nao um stub


def test_payload_sobrevive_ao_ciclo_de_ida_e_volta(monkeypatch, repo):
    respostas = iter([resposta_com_tool_call(), resposta_final()])
    monkeypatch.setattr(
        agente.CLIENT.chat.completions, "create", lambda **kw: next(respostas)
    )
    primeiro = agente.rodar_agente(CLIENTE, FLAGS_BASE, cache=repo)
    segundo = agente.rodar_agente(CLIENTE, FLAGS_BASE, cache=repo)

    assert segundo["cache_hit"] is True
    assert segundo["tools_chamadas"] == primeiro["tools_chamadas"]


def test_ordem_das_evidencias_e_preservada(repo):
    repo.salvar("h-ordem", {
        "cliente_id": CLIENTE, "parecer": None, "erro_parsing": "x",
        "texto_bruto": None, "tokens_total": 1, "latencia_s": 0.1,
        "tools_chamadas": [
            {"tool": "historico_cliente", "args": {"cliente_id": CLIENTE}, "payload": {"a": 1}},
            {"tool": "operacoes_do_dia", "args": {"cliente_id": CLIENTE, "data": "2026-05-26"},
             "payload": {"b": 2}},
            {"tool": "perfil_canal", "args": {"cliente_id": CLIENTE}, "payload": {"c": 3}},
        ],
    })
    recuperado = repo.obter("h-ordem")
    assert [c["tool"] for c in recuperado["tools_chamadas"]] == [
        "historico_cliente", "operacoes_do_dia", "perfil_canal"
    ]
    assert recuperado["tools_chamadas"][1]["payload"] == {"b": 2}
