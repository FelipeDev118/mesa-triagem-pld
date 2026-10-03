"""Passos 0.6 e 0.7 - execucao de regras materializada e fila de alertas.

O criterio mais forte esta em test_nivel_risco_regra_bate_com_o_confronto_da_entrega:
o store nao pode inventar uma classificacao diferente da que a entrega ja
produziu para os mesmos 30 clientes.
"""
import csv
import json

import pytest

from dados import PARAMETROS_REGRAS
from mesa import db, regras_run
from mesa.ingestao import ingerir

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"
CONFRONTO_CSV = db.RAIZ / "outputs" / "confronto_regra_vs_agente.csv"


@pytest.fixture
def conn(tmp_path):
    c = db.conectar(tmp_path / "mesa.db")
    ingerir(c, DADOS_REAIS)
    yield c
    c.close()


# ---------- 0.6 ----------


def test_execucao_reproduz_os_numeros_da_entrega(conn):
    r = regras_run.executar(conn)
    assert r.operacoes_avaliadas == 317
    assert r.clientes_fracionamento == 4
    assert r.operacoes_atipicas == 21


def test_sinalizacoes_gravadas_batem_com_as_regras(conn):
    regras_run.executar(conn)
    por_regra = dict(
        conn.execute("SELECT regra, COUNT(*) FROM sinalizacoes GROUP BY regra").fetchall()
    )
    assert por_regra["valor_atipico"] == 21
    # 4 disparos de fracionamento, um por (cliente, dia) - coincide com 4 clientes
    # nesta base porque nenhum cliente fraciona em mais de um dia
    assert por_regra["fracionamento"] == 4
    assert conn.execute(
        "SELECT COUNT(DISTINCT cliente_id) FROM sinalizacoes WHERE regra='fracionamento'"
    ).fetchone()[0] == 4


def test_sinalizacao_de_fracionamento_tem_data_e_nao_operacao(conn):
    """A regra e por DIA: apontar uma operacao unica seria mentir sobre o que
    disparou. A de valor atipico e o inverso."""
    regras_run.executar(conn)
    frac = conn.execute(
        "SELECT * FROM sinalizacoes WHERE regra='fracionamento' LIMIT 1"
    ).fetchone()
    assert frac["operacao_id"] is None
    assert frac["data"] is not None
    detalhe = json.loads(frac["detalhe_json"])
    assert detalhe["qtd_operacoes"] >= PARAMETROS_REGRAS["frac_min_ops"]
    assert detalhe["soma_do_dia"] > PARAMETROS_REGRAS["frac_soma_min"]
    assert detalhe["max_individual"] < PARAMETROS_REGRAS["frac_max_individual"]

    atip = conn.execute(
        "SELECT * FROM sinalizacoes WHERE regra='valor_atipico' LIMIT 1"
    ).fetchone()
    assert atip["operacao_id"] is not None
    assert atip["data"] is None
    detalhe = json.loads(atip["detalhe_json"])
    assert detalhe["valor_brl"] > detalhe["limite_atipico"]


def test_parametros_ficam_gravados_na_execucao(conn):
    """O ponto do passo 0.5: um alerta antigo continua explicavel depois que o
    limiar mudar."""
    regras_run.executar(conn)
    linha = conn.execute("SELECT * FROM execucoes_regras").fetchone()
    assert json.loads(linha["parametros_json"]) == PARAMETROS_REGRAS
    assert linha["versao_regras"] == "r1-2026-09-12"


def test_execucao_com_parametros_diferentes_muda_o_resultado_e_fica_registrada(conn):
    padrao = regras_run.executar(conn)
    apertado = regras_run.executar(conn, {**PARAMETROS_REGRAS, "atipico_fator": 10})

    assert apertado.operacoes_atipicas < padrao.operacoes_atipicas
    # as duas execucoes coexistem: a antiga nao e sobrescrita
    assert conn.execute("SELECT COUNT(*) FROM execucoes_regras").fetchone()[0] == 2
    fatores = [
        json.loads(r[0])["atipico_fator"]
        for r in conn.execute("SELECT parametros_json FROM execucoes_regras ORDER BY id")
    ]
    assert fatores == [5, 10]


def test_datas_fracionamento_do_store_bate_com_o_calculo(conn):
    """A consulta tem que dar o mesmo que o recalculo que ela substitui."""
    from dados import aplicar_regras, datas_fracionamento
    from mesa import repositorio

    regras_run.executar(conn)
    df = aplicar_regras(repositorio.operacoes_df(conn))

    clientes = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT cliente_id FROM sinalizacoes WHERE regra='fracionamento'"
        )
    ]
    assert clientes, "nenhum cliente com fracionamento - teste nao exercita nada"
    for cliente_id in clientes:
        assert regras_run.datas_fracionamento_do_store(conn, cliente_id) == \
            datas_fracionamento(df, cliente_id)


def test_regras_sem_ingestao_falham_claro(tmp_path):
    c = db.conectar(tmp_path / "vazio.db")
    with pytest.raises(RuntimeError, match="store vazio"):
        regras_run.executar(c)
    c.close()


# ---------- 0.7 ----------


def test_alertas_separam_regra_de_controle(conn):
    r = regras_run.executar(conn)
    assert r.alertas_regra == 17
    assert r.alertas_controle == 13
    assert conn.execute("SELECT COUNT(*) FROM alertas").fetchone()[0] == 30

    sem_sinal = conn.execute(
        "SELECT COUNT(*) FROM alertas WHERE origem='controle' AND total_sinalizacoes != 0"
    ).fetchone()[0]
    assert sem_sinal == 0


def test_nivel_risco_regra_bate_com_o_confronto_da_entrega(conn):
    """<<< criterio de aceite do 0.7 >>>

    O store nao pode classificar diferente do que a entrega ja produziu para os
    mesmos clientes. A fonte da verdade e o CSV commitado, nao um numero que eu
    recalcule aqui - recalcular com o mesmo codigo provaria so que o codigo
    concorda consigo mesmo.
    """
    regras_run.executar(conn)
    do_store = dict(
        conn.execute("SELECT cliente_id, nivel_risco_regra FROM alertas").fetchall()
    )

    with open(CONFRONTO_CSV, encoding="utf-8") as f:
        da_entrega = {
            linha["cliente_id"]: linha["nivel_risco_esperado_regra"]
            for linha in csv.DictReader(f)
        }

    assert len(da_entrega) == 30
    assert do_store == da_entrega


def test_alerta_nasce_no_estado_novo_e_sem_dono(conn):
    regras_run.executar(conn)
    pendentes = conn.execute(
        "SELECT COUNT(*) FROM alertas WHERE estado='novo' AND analista_id IS NULL"
    ).fetchone()[0]
    assert pendentes == 30


def test_um_alerta_por_cliente_por_execucao(conn):
    import sqlite3

    r = regras_run.executar(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO alertas (execucao_id, cliente_id, origem, "
            "sinalizacoes_fracionamento, sinalizacoes_valor_atipico, total_sinalizacoes, "
            "volume_total_brl, qtd_operacoes, nivel_risco_regra, criado_em) "
            "VALUES (?, 'CLI-014', 'regra', 0, 1, 1, 1.0, 1, 'médio', '2026-01-01T00:00:00Z')",
            (r.execucao_id,),
        )


def test_fila_ordena_pelo_mesmo_criterio_do_ranking(conn):
    """A fila do analista tem que sair na ordem do ranking da entrega."""
    from dados import aplicar_regras, ranking_clientes_sinalizados
    from mesa import repositorio

    regras_run.executar(conn)
    fila = [
        r[0] for r in conn.execute(
            "SELECT cliente_id FROM alertas WHERE origem='regra' AND estado='novo' "
            "ORDER BY total_sinalizacoes DESC, volume_total_brl DESC LIMIT 10"
        )
    ]
    df = aplicar_regras(repositorio.operacoes_df(conn))
    assert fila == ranking_clientes_sinalizados(df, top_n=10)["cliente_id"].tolist()


# ---------- 5.1: execucao incremental ----------


def _lote(tmp_path, conn, operacoes, nome="lote2.json"):
    caminho = tmp_path / nome
    caminho.write_text(json.dumps({"taxa_cambio_usd_brl": 5.4, "operacoes": operacoes}),
                       encoding="utf-8")
    return ingerir(conn, caminho)


def _op_real(op_id, **mudancas):
    ops = json.loads(DADOS_REAIS.read_text(encoding="utf-8"))["operacoes"]
    return {**next(o for o in ops if o["id"] == op_id), **mudancas}


def test_regras_sobre_o_delta_sao_as_mesmas_da_base_inteira(conn):
    """A garantia que SUSTENTA a execucao incremental: as regras sao por
    cliente, entao rodar sobre o recorte de um cliente da o mesmo resultado que
    rodar sobre a base inteira e olhar so aquele cliente. Conferido para os 30.
    Se um dia entrar uma regra entre clientes, este teste quebra - e o delta
    tem que alargar, nao o teste afrouxar."""
    from dados import aplicar_regras
    from mesa import repositorio

    df = repositorio.operacoes_df(conn)
    inteira = aplicar_regras(df).set_index("id")
    colunas = ["flag_fracionamento", "flag_valor_atipico"]
    for cliente in df["cliente_id"].unique():
        recorte = aplicar_regras(df[df["cliente_id"] == cliente]).set_index("id")
        assert recorte[colunas].equals(inteira.loc[recorte.index, colunas]), cliente


def test_primeira_execucao_e_completa_e_repetir_nao_faz_nada(conn):
    primeira = regras_run.executar(conn)
    assert (primeira.escopo, primeira.alertas_regra + primeira.alertas_controle) == ("completa", 30)
    assert regras_run.executar(conn) is None
    # reingerir o MESMO arquivo tambem nao e delta
    ingerir(conn, DADOS_REAIS)
    assert regras_run.executar(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM execucoes_regras").fetchone()[0] == 1


def test_lote_novo_gera_execucao_so_para_os_clientes_afetados(conn, tmp_path):
    regras_run.executar(conn)
    _lote(tmp_path, conn, [
        _op_real("OP-00269", valor=7000.0),                          # correcao, CLI-028
        _op_real("OP-00001", id="OP-90001", cliente_id="CLI-011"),  # nova, CLI-011
    ])
    r = regras_run.executar(conn)
    assert r.escopo == "incremental"
    novos = conn.execute(
        "SELECT cliente_id, substitui_alerta_id FROM alertas WHERE execucao_id = ? ORDER BY cliente_id",
        (r.execucao_id,),
    ).fetchall()
    assert [n[0] for n in novos] == ["CLI-011", "CLI-028"]
    assert all(n[1] is not None for n in novos)          # cada um substitui o anterior
    vigentes = conn.execute(
        f"SELECT COUNT(*) FROM alertas a WHERE {__import__('mesa.repositorio', fromlist=['x']).VIGENTE}"
    ).fetchone()[0]
    assert vigentes == 30                                # um por cliente, sempre


def test_correcao_que_move_operacao_de_cliente_reavalia_os_dois(conn, tmp_path):
    regras_run.executar(conn)
    antes = _op_real("OP-00269")["cliente_id"]
    _lote(tmp_path, conn, [_op_real("OP-00269", cliente_id="CLI-011")])
    r = regras_run.executar(conn)
    clientes = {c for (c,) in conn.execute(
        "SELECT cliente_id FROM alertas WHERE execucao_id = ?", (r.execucao_id,))}
    assert clientes == {antes, "CLI-011"}


def test_parametros_diferentes_forcam_execucao_completa(conn):
    regras_run.executar(conn)
    outros = {**PARAMETROS_REGRAS, "atipico_fator": 4}
    r = regras_run.executar(conn, outros)
    assert r.escopo == "completa"
    assert r.alertas_regra + r.alertas_controle == 30
