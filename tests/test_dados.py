"""Testes das regras determinísticas (nivel_2/dados.py), focados nos limites onde
regra de negócio quebra: ver docs/DECISOES.md, seção "Testes automatizados das regras".
"""
import pandas as pd
import pytest

from conftest import escrever_dataset, op
from dados import (
    PARAMETROS_REGRAS,
    aplicar_regras,
    carregar_e_limpar,
    datas_fracionamento,
    flag_fracionamento,
    flag_valor_atipico,
    montar_flags,
    ranking_clientes_sinalizados,
    todos_os_clientes,
)


# ---------- carregar_e_limpar: limpeza e conversão ----------


def test_conversao_usd_para_brl_usa_taxa_do_arquivo(tmp_path):
    caminho = escrever_dataset(tmp_path, [op("OP-1", "CLI-1", "2026-01-01", 100.0, moeda="USD")], taxa=5.0)
    df, taxa = carregar_e_limpar(caminho)
    assert taxa == 5.0
    assert df.loc[df["id"] == "OP-1", "valor_brl"].iloc[0] == pytest.approx(500.0)


def test_operacao_brl_nao_e_convertida(tmp_path):
    caminho = escrever_dataset(tmp_path, [op("OP-1", "CLI-1", "2026-01-01", 100.0, moeda="BRL")], taxa=5.0)
    df, _ = carregar_e_limpar(caminho)
    assert df.loc[df["id"] == "OP-1", "valor_brl"].iloc[0] == pytest.approx(100.0)


def test_data_invalida_marca_data_valida_false(tmp_path):
    caminho = escrever_dataset(tmp_path, [op("OP-1", "CLI-1", "data-invalida", 100.0)])
    df, _ = carregar_e_limpar(caminho)
    assert bool(df.loc[df["id"] == "OP-1", "data_valida"].iloc[0]) is False


def test_duplicata_por_id_e_removida(tmp_path):
    caminho = escrever_dataset(
        tmp_path,
        [op("OP-1", "CLI-1", "2026-01-01", 100.0), op("OP-1", "CLI-1", "2026-01-01", 100.0)],
    )
    df, _ = carregar_e_limpar(caminho)
    assert len(df) == 1


# ---------- Regra 1: fracionamento (flag_fracionamento) ----------


def _df_um_dia(valores, cliente_id="CLI-1", data="2026-01-01"):
    return pd.DataFrame(
        [
            {"cliente_id": cliente_id, "data": pd.Timestamp(data), "valor_brl": v, "data_valida": True, "id": f"OP-{i}"}
            for i, v in enumerate(valores)
        ]
    )


def test_fracionamento_exige_pelo_menos_3_operacoes_no_dia():
    # 2 operacoes, cada uma < 20000, somando > 50000: nao deveria fracionar por qtd
    df = _df_um_dia([19000.0, 32000.0])
    assert flag_fracionamento(df).empty


def test_fracionamento_com_exatamente_3_operacoes_dispara():
    df = _df_um_dia([19000.0, 19000.0, 19000.0])
    candidatos = flag_fracionamento(df)
    assert len(candidatos) == 1
    assert candidatos.iloc[0]["qtd"] == 3


def test_soma_exatamente_50000_nao_dispara_fracionamento():
    valores = [16666.66, 16666.67, 16666.67]
    assert sum(valores) == pytest.approx(50000.0)
    assert flag_fracionamento(_df_um_dia(valores)).empty


def test_soma_logo_acima_de_50000_dispara_fracionamento():
    valores = [16700.0, 16700.0, 16700.0]  # soma = 50100.0
    candidatos = flag_fracionamento(_df_um_dia(valores))
    assert len(candidatos) == 1


def test_operacao_de_exatamente_20000_invalida_o_grupo():
    # max_individual < 20000 e estrito: uma operacao de exatamente 20000 no grupo
    # tira o caso da regra, mesmo com qtd e soma satisfeitas
    valores = [20000.0, 16000.0, 16000.0]  # qtd=3, soma=52000 > 50000, max=20000
    assert flag_fracionamento(_df_um_dia(valores)).empty


def test_operacao_logo_abaixo_de_20000_mantem_o_grupo():
    valores = [19999.99, 16000.0, 16000.0]
    candidatos = flag_fracionamento(_df_um_dia(valores))
    assert len(candidatos) == 1


def test_cliente_com_todas_as_datas_nulas_nao_frac_e_nao_quebra(tmp_path):
    caminho = escrever_dataset(tmp_path, [op(f"OP-{i}", "CLI-1", "data-invalida", 20000.0) for i in range(3)])
    df, _ = carregar_e_limpar(caminho)
    assert flag_fracionamento(df).empty
    resultado = aplicar_regras(df)  # nao pode lancar excecao com 0 datas validas
    assert not resultado["flag_fracionamento"].any()


def test_datas_fracionamento_retorna_dias_ordenados(tmp_path):
    ops = [op(f"OP-{i}", "CLI-1", "2026-01-05", 19000.0) for i in range(3)] + [
        op(f"OP-{i + 3}", "CLI-1", "2026-01-01", 19000.0) for i in range(3)
    ]
    caminho = escrever_dataset(tmp_path, ops)
    df, _ = carregar_e_limpar(caminho)
    assert datas_fracionamento(df, "CLI-1") == ["2026-01-01", "2026-01-05"]


# ---------- Regra 2: valor atípico (flag_valor_atipico) ----------


def _df_cliente(valores, cliente_id="CLI-1"):
    return pd.DataFrame(
        [{"id": f"OP-{i}", "cliente_id": cliente_id, "valor_brl": v} for i, v in enumerate(valores)]
    )


def test_cliente_com_3_operacoes_nao_e_elegivel_para_atipico():
    df = _df_cliente([100.0, 100.0, 100000.0])
    assert flag_valor_atipico(df).empty


def test_cliente_com_exatamente_4_operacoes_e_elegivel_para_atipico():
    df = _df_cliente([100.0, 100.0, 100.0, 100000.0])
    atipicos = flag_valor_atipico(df)
    assert len(atipicos) == 4
    assert bool(atipicos.loc[atipicos["id"] == "OP-3", "atipico"].iloc[0]) is True


def test_valor_exatamente_no_limite_nao_e_atipico():
    # mediana([100,100,100,500]) = 100; limite = 500; valor == limite nao marca (estrito >)
    df = _df_cliente([100.0, 100.0, 100.0, 500.0])
    atipicos = flag_valor_atipico(df)
    assert bool(atipicos.loc[atipicos["id"] == "OP-3", "atipico"].iloc[0]) is False


def test_valor_logo_acima_do_limite_e_atipico():
    df = _df_cliente([100.0, 100.0, 100.0, 500.01])
    atipicos = flag_valor_atipico(df)
    assert bool(atipicos.loc[atipicos["id"] == "OP-3", "atipico"].iloc[0]) is True


# ---------- montar_flags ----------


def test_montar_flags_sem_fracionamento_nao_inclui_datas():
    df = pd.DataFrame({"cliente_id": ["CLI-1"], "data": [pd.Timestamp("2026-01-01")]})
    row = pd.Series({"cliente_id": "CLI-1", "sinalizacoes_fracionamento": 0, "sinalizacoes_valor_atipico": 2})
    assert montar_flags(df, row) == {"flag_fracionamento": False, "flag_valor_atipico": True}


def test_montar_flags_com_fracionamento_inclui_datas(tmp_path):
    caminho = escrever_dataset(tmp_path, [op(f"OP-{i}", "CLI-1", "2026-01-05", 19000.0) for i in range(3)])
    df, _ = carregar_e_limpar(caminho)
    row = pd.Series({"cliente_id": "CLI-1", "sinalizacoes_fracionamento": 1, "sinalizacoes_valor_atipico": 0})
    assert montar_flags(df, row) == {
        "flag_fracionamento": True,
        "flag_valor_atipico": False,
        "datas_fracionamento": ["2026-01-05"],
    }


# ---------- ranking_clientes_sinalizados ----------


def test_ranking_desempata_por_volume_total(tmp_path):
    ops = [op(f"A{i}", "CLI-A", "2026-01-01", 19000.0) for i in range(3)] + [
        op(f"B{i}", "CLI-B", "2026-01-01", 19500.0) for i in range(3)
    ]
    caminho = escrever_dataset(tmp_path, ops)
    df, _ = carregar_e_limpar(caminho)
    df = aplicar_regras(df)
    ranking = ranking_clientes_sinalizados(df, top_n=10)
    assert list(ranking["cliente_id"]) == ["CLI-B", "CLI-A"]


# ---------- todos_os_clientes ----------


def test_todos_os_clientes_inclui_quem_nao_foi_sinalizado(tmp_path):
    # CLI-A: fracionamento (sinalizado). CLI-B: 1 operacao normal, nenhuma flag.
    ops = [op(f"A{i}", "CLI-A", "2026-01-01", 19000.0) for i in range(3)] + [
        op("B0", "CLI-B", "2026-01-01", 500.0)
    ]
    caminho = escrever_dataset(tmp_path, ops)
    df, _ = carregar_e_limpar(caminho)
    df = aplicar_regras(df)

    sinalizados = ranking_clientes_sinalizados(df, top_n=10)
    assert list(sinalizados["cliente_id"]) == ["CLI-A"]  # CLI-B fica de fora

    todos = todos_os_clientes(df)
    assert set(todos["cliente_id"]) == {"CLI-A", "CLI-B"}
    linha_b = todos[todos["cliente_id"] == "CLI-B"].iloc[0]
    assert linha_b["total_sinalizacoes"] == 0
    # sinalizado (CLI-A) vem antes do nao sinalizado (CLI-B) na ordenacao
    assert list(todos["cliente_id"]) == ["CLI-A", "CLI-B"]


# ---------- PARAMETROS_REGRAS (passo 0.5 do ROADMAP) ----------


def test_parametros_regras_tem_os_valores_do_enunciado():
    """Guarda contra alterar um limiar sem perceber: estes numeros vem do
    enunciado e sustentam todos os testes de fronteira acima."""
    from dados import PARAMETROS_REGRAS

    assert PARAMETROS_REGRAS == {
        "frac_min_ops": 3,
        "frac_soma_min": 50000,
        "frac_max_individual": 20000,
        "atipico_min_ops": 4,
        "atipico_fator": 5,
    }


def test_parametro_de_fracionamento_e_realmente_usado():
    """Extrair para uma constante nao vale nada se a funcao continuar decidindo
    pelo numero magico. Baixar o limiar tem que mudar o resultado."""
    # 3 operacoes de 19k: soma 57k > 50k, nenhuma atinge 20k -> dispara no padrao.
    # (Nao da para testar afrouxando min_ops para 2: com max_individual < 20000,
    # duas operacoes nunca somam mais de 50000 - o cenario e impossivel por
    # construcao, e a primeira versao deste teste caiu exatamente nisso.)
    df = _df_um_dia([19000.0, 19000.0, 19000.0])
    assert len(flag_fracionamento(df)) == 1

    apertado = {**PARAMETROS_REGRAS, "frac_min_ops": 4}
    assert flag_fracionamento(df, apertado).empty


def test_parametro_de_atipicidade_e_realmente_usado():
    df = _df_um_dia([100.0, 100.0, 100.0, 100.0, 400.0])  # 400 = 4x a mediana
    assert not flag_valor_atipico(df)["atipico"].any()

    apertado = {**PARAMETROS_REGRAS, "atipico_fator": 3}
    assert flag_valor_atipico(df, apertado)["atipico"].sum() == 1


def test_data_fora_do_formato_iso_vira_invalida_e_nao_data_errada(tmp_path):
    """Sem formato fixo, o pandas lia "05/03/2026" como 3 de maio (mes primeiro)
    num arquivo so com datas brasileiras. Para PLD, dia errado e pior que dia
    ausente: a Regra 1 soma operacoes POR DIA."""
    caminho = escrever_dataset(tmp_path, [op("OP-1", "CLI-1", "05/03/2026", 100.0),
                                          op("OP-2", "CLI-1", "13/03/2026", 100.0)])
    df, _ = carregar_e_limpar(caminho)
    assert not df["data_valida"].any()
    assert df["data"].isna().all()
