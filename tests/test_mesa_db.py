"""Passo 0.1 - conexao e esquema.

O que estes testes protegem: FK ligada (o SQLite deixa DESLIGADA por padrao, e
por conexao - declarar REFERENCES no DDL sem ligar o PRAGMA e ter integridade
so no comentario) e idempotencia do esquema.
"""
import sqlite3

import pytest

from mesa import db


def test_conectar_cria_o_arquivo_e_as_tabelas(tmp_path):
    conn = db.conectar(tmp_path / "mesa.db")
    tabelas = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "lotes_ingestao",
        "operacoes",
        "execucoes_regras",
        "sinalizacoes",
        "alertas",
    } <= tabelas
    conn.close()


def test_foreign_keys_estao_ligadas(tmp_path):
    conn = db.conectar(tmp_path / "mesa.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_fk_invalida_e_rejeitada(tmp_path):
    """Nao basta o PRAGMA responder 1: a restricao tem que barrar de verdade."""
    conn = db.conectar(tmp_path / "mesa.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operacoes (id, lote_id, cliente_id, data, data_valida, "
            "valor, moeda, valor_brl, canal, tipo, contraparte) "
            "VALUES ('OP-1', 999, 'CLI-1', '2026-01-01', 1, 10.0, 'BRL', 10.0, "
            "'pix', 'pagamento', 'Fulano')"
        )
        conn.commit()
    conn.close()


def test_aplicar_esquema_duas_vezes_nao_quebra(tmp_path):
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    conn.execute(
        "INSERT INTO lotes_ingestao (origem, sha256_arquivo, taxa_cambio_usd_brl, "
        "operacoes_brutas, operacoes_inseridas, duplicatas_ignoradas, "
        "datas_nulas_brutas, operacoes_usd_brutas, ingerido_em) "
        "VALUES ('x.json', 'abc', 5.4, 1, 1, 0, 0, 0, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # reabrir aplica o esquema de novo; o dado tem que continuar la
    conn = db.conectar(caminho)
    assert conn.execute("SELECT COUNT(*) FROM lotes_ingestao").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSAO_ESQUEMA
    conn.close()


def test_banco_de_versao_futura_e_recusado(tmp_path):
    """Escrever num esquema que este codigo nao entende e pior que falhar."""
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    conn.execute(f"PRAGMA user_version = {db.VERSAO_ESQUEMA + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="mais antigo que o banco"):
        db.conectar(caminho)


def test_estado_de_alerta_invalido_e_rejeitado(tmp_path):
    """O CHECK do enum vive no schema, nao na confianca de quem faz o INSERT."""
    conn = db.conectar(tmp_path / "mesa.db")
    conn.execute(
        "INSERT INTO lotes_ingestao (id, origem, sha256_arquivo, taxa_cambio_usd_brl, "
        "operacoes_brutas, operacoes_inseridas, duplicatas_ignoradas, "
        "datas_nulas_brutas, operacoes_usd_brutas, ingerido_em) "
        "VALUES (1, 'x.json', 'abc', 5.4, 1, 1, 0, 0, 0, '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO execucoes_regras (id, lote_id, versao_regras, parametros_json, "
        "executado_em, operacoes_avaliadas, clientes_fracionamento, operacoes_atipicas) "
        "VALUES (1, 1, 'r1', '{}', '2026-01-01T00:00:00Z', 1, 0, 0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO alertas (execucao_id, cliente_id, origem, "
            "sinalizacoes_fracionamento, sinalizacoes_valor_atipico, total_sinalizacoes, "
            "volume_total_brl, qtd_operacoes, nivel_risco_regra, estado, criado_em) "
            "VALUES (1, 'CLI-1', 'regra', 0, 1, 1, 100.0, 3, 'médio', 'inventado', "
            "'2026-01-01T00:00:00Z')"
        )
    conn.close()


# ============================================================================
# Passos 4.0 e 5.0 - migracao por versao
# ============================================================================

ESQUEMAS = db.RAIZ / "tests" / "esquemas"


def _banco_na_versao(caminho, versao: int):
    """Um banco criado com o DDL CONGELADO daquela versao (copiado do git no dia
    em que a versao seguinte nasceu) - nao reconstruido a partir do DDL atual,
    que ja nao e o que existia."""
    conn = sqlite3.connect(caminho)
    conn.executescript((ESQUEMAS / f"esquema_v{versao}.sql").read_text(encoding="utf-8"))
    conn.execute(f"PRAGMA user_version = {versao}")
    conn.execute(
        "INSERT INTO lotes_ingestao (id, origem, sha256_arquivo, taxa_cambio_usd_brl, "
        "operacoes_brutas, operacoes_inseridas, duplicatas_ignoradas, "
        "datas_nulas_brutas, operacoes_usd_brutas, ingerido_em) "
        "VALUES (1, 'x.json', 'abc', 5.4, 1, 1, 0, 0, 0, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()


def _estrutura(conn) -> dict:
    """Colunas (nome, tipo, not null, default, pk), FKs, indices com suas
    colunas e o SQL dos triggers. O texto do CREATE TABLE fica de fora de
    proposito: um ALTER ADD COLUMN reescreve o texto de outro jeito, e o que
    importa e o que o banco FAZ, nao como o comando foi digitado."""
    est = {}
    for (tabela,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        est[f"t:{tabela}"] = (
            [tuple(r)[1:] for r in conn.execute(f"PRAGMA table_info({tabela})")],
            sorted(tuple(r)[2:5] for r in conn.execute(f"PRAGMA foreign_key_list({tabela})")),
        )
    for nome, tabela in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ):
        est[f"i:{nome}"] = (tabela, [tuple(r)[2] for r in conn.execute(f"PRAGMA index_info({nome})")])
    for nome, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'"):
        est[f"g:{nome}"] = sql
    return est


@pytest.mark.parametrize("versao", [5, 6, 7])
def test_banco_antigo_migrado_tem_a_estrutura_de_um_banco_novo(tmp_path, versao):
    """<<< aceite do 5.0 >>> A garantia que faz a migracao ser confiavel: nao
    "as tabelas novas existem", mas "o banco migrado e indistinguivel de um
    criado agora" - colunas, tipos, defaults, FKs, indices e triggers."""
    _banco_na_versao(tmp_path / "velho.db", versao)
    migrado = db.conectar(tmp_path / "velho.db")
    novo = db.conectar(tmp_path / "novo.db")
    assert _estrutura(migrado) == _estrutura(novo)
    assert migrado.execute("PRAGMA user_version").fetchone()[0] == db.VERSAO_ESQUEMA
    assert migrado.execute("SELECT COUNT(*) FROM lotes_ingestao").fetchone()[0] == 1
    migrado.close()
    novo.close()


def test_toda_versao_desde_a_v5_tem_migracao():
    assert set(range(5, db.VERSAO_ESQUEMA)) <= set(db.MIGRACOES)


def test_coluna_nova_da_migracao_mantem_o_check(tmp_path):
    """O ALTER da v7 traz um CHECK; num banco migrado ele tem que valer igual."""
    _banco_na_versao(tmp_path / "velho.db", 6)
    conn = db.conectar(tmp_path / "velho.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execucoes_regras (lote_id, versao_regras, parametros_json, executado_em, "
            "operacoes_avaliadas, clientes_fracionamento, operacoes_atipicas, escopo) "
            "VALUES (1, 'r1', '{}', 'x', 1, 0, 0, 'parcial')"
        )
    conn.close()


def test_migracao_que_falha_no_meio_nao_deixa_o_banco_pela_metade(tmp_path, monkeypatch):
    """Os dois ALTER da v7 numa transacao so, com o carimbo da versao: se o
    segundo falha, o primeiro tambem nao fica."""
    _banco_na_versao(tmp_path / "velho.db", 6)
    monkeypatch.setitem(db.MIGRACOES, 6, [db.MIGRACOES[6][0], "ALTER TABLE nao_existe ADD COLUMN x"])
    with pytest.raises(sqlite3.OperationalError):
        db.conectar(tmp_path / "velho.db")
    bruta = sqlite3.connect(tmp_path / "velho.db")
    colunas = [r[1] for r in bruta.execute("PRAGMA table_info(execucoes_regras)")]
    assert "escopo" not in colunas
    assert bruta.execute("PRAGMA user_version").fetchone()[0] == 6
    bruta.close()


def test_banco_de_versao_sem_migracao_e_recusado(tmp_path):
    """A brecha fechada no 4.0: aplicar_esquema() num banco v4 so carimbava a
    versao nova - e a coluna aderencia.marcas_json (v5) nao aparecia, porque
    CREATE TABLE IF NOT EXISTS nao altera tabela existente."""
    caminho = tmp_path / "mesa.db"
    _banco_na_versao(caminho, 5)
    bruta = sqlite3.connect(caminho)
    bruta.execute("PRAGMA user_version = 4")
    bruta.commit()
    bruta.close()

    with pytest.raises(RuntimeError, match="nao ha migracao"):
        db.conectar(caminho)
    bruta = sqlite3.connect(caminho)
    assert bruta.execute("PRAGMA user_version").fetchone()[0] == 4  # nao carimbou
    bruta.close()


def _alerta_minimo(conn):
    conn.execute(
        "INSERT INTO lotes_ingestao (id, origem, sha256_arquivo, taxa_cambio_usd_brl, "
        "operacoes_brutas, operacoes_inseridas, duplicatas_ignoradas, "
        "datas_nulas_brutas, operacoes_usd_brutas, ingerido_em) "
        "VALUES (1, 'x.json', 'abc', 5.4, 1, 1, 0, 0, 0, '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO execucoes_regras (id, lote_id, versao_regras, parametros_json, "
        "executado_em, operacoes_avaliadas, clientes_fracionamento, operacoes_atipicas) "
        "VALUES (1, 1, 'r1', '{}', '2026-01-01T00:00:00Z', 1, 0, 0)"
    )
    conn.execute(
        "INSERT INTO alertas (id, execucao_id, cliente_id, origem, "
        "sinalizacoes_fracionamento, sinalizacoes_valor_atipico, total_sinalizacoes, "
        "volume_total_brl, qtd_operacoes, nivel_risco_regra, estado, criado_em) "
        "VALUES (1, 1, 'CLI-1', 'regra', 0, 1, 1, 100.0, 3, 'médio', 'concluido', "
        "'2026-01-01T00:00:00Z')"
    )


def test_transicoes_e_decisoes_sao_append_only(tmp_path):
    conn = db.conectar(tmp_path / "mesa.db")
    _alerta_minimo(conn)
    conn.execute(
        "INSERT INTO transicoes (alerta_id, estado_anterior, estado_novo, ator, ator_tipo, "
        "registrado_em) VALUES (1, 'em_analise', 'concluido', 'ana', 'analista', 'x')"
    )
    conn.execute(
        "INSERT INTO decisoes (alerta_id, analista_id, decisao, nivel_risco_analista, "
        "motivo, decidido_em) VALUES (1, 'ana', 'discordo', 'alto', 'porque sim', 'x')"
    )
    conn.commit()

    for sql in ("UPDATE transicoes SET ator = 'bruno'", "DELETE FROM transicoes",
                "UPDATE decisoes SET decisao = 'concordo'", "DELETE FROM decisoes"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
    conn.close()


@pytest.mark.parametrize("decisao,analista,agente,motivo", [
    ("concordo", "alto", None, None),        # concordar com o que? nao ha nivel do agente
    ("concordo", "alto", "médio", None),     # "concordo" com nivel diferente do agente
    ("discordo", None, "médio", "x"),        # discordar sem dizer qual nivel
    ("discordo", "alto", "médio", "   "),    # discordar sem motivo
    ("escalar", None, "médio", None),        # escalar sem motivo
    ("discordo", "medio", "alto", "x"),      # nivel fora do vocabulario (sem acento)
    # os tres abaixo pegaram um bug real: CHECK com expressao NULL passa
    ("concordo", None, "médio", None),       # concordo sem nivel do analista
    ("discordo", "alto", "médio", None),     # discordar com motivo NULL
])
def test_regras_de_cada_decisao_valem_no_schema(tmp_path, decisao, analista, agente, motivo):
    """A API valida antes, mas a regra tem que valer para qualquer INSERT."""
    conn = db.conectar(tmp_path / "mesa.db")
    _alerta_minimo(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisoes (alerta_id, analista_id, decisao, nivel_risco_analista, "
            "nivel_risco_agente, motivo, decidido_em) VALUES (1, 'ana', ?, ?, ?, ?, 'x')",
            (decisao, analista, agente, motivo),
        )
    conn.close()


def test_uma_decisao_por_caso(tmp_path):
    conn = db.conectar(tmp_path / "mesa.db")
    _alerta_minimo(conn)
    sql = ("INSERT INTO decisoes (alerta_id, analista_id, decisao, motivo, decidido_em) "
           "VALUES (1, ?, 'escalar', 'grave', 'x')")
    conn.execute(sql, ("ana",))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(sql, ("bruno",))
    conn.close()


def test_verificar_e_confrontar_o_store_nao_migram_o_banco(tmp_path, monkeypatch):
    """Bug real da Fase 5: `verificar_ambiente.py --store` abria o banco com
    conectar(), que migra - e migrou o store real de v6 para v7 enquanto um
    servidor da versao anterior lia dele (que passou a responder 503). Comando
    de leitura recusa versao diferente e deixa o banco como estava."""
    import confronto
    import verificar_ambiente

    caminho = tmp_path / "mesa.db"
    _banco_na_versao(caminho, 6)
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)

    for ler in (verificar_ambiente.obter_do_store, confronto.carregar_do_store):
        with pytest.raises(SystemExit, match="python -m mesa.db"):
            ler()
    bruta = sqlite3.connect(caminho)
    assert bruta.execute("PRAGMA user_version").fetchone()[0] == 6
    bruta.close()


def test_caminho_padrao_e_lido_na_hora_da_chamada(tmp_path, monkeypatch):
    """Achado da auditoria: `conectar(caminho=CAMINHO_PADRAO)` fixava o padrao
    na importacao. Um script que trocou db.CAMINHO_PADRAO para um banco
    temporario e chamou conectar() sem argumento escreveu no store REAL."""
    alvo = tmp_path / "outro.db"
    monkeypatch.setattr(db, "CAMINHO_PADRAO", alvo)
    db.conectar().close()
    assert alvo.exists()
    db.abrir_para_leitura().close()
