"""Fase 4.3 - tempo de analise pela trilha, sem banco.

As bordas que importam: caso devolvido e repego (dois periodos), trilha que
comeca no meio (caso pego antes do esquema v6) e caso ainda nao concluido.
"""
from mesa.metricas import tempo_em_analise


def passo(de, para, hora):
    return {"estado_anterior": de, "estado_novo": para, "registrado_em": f"2026-09-27T{hora}Z"}


def test_um_periodo():
    assert tempo_em_analise([
        passo("novo", "triado", "08:00:00"),       # triagem nao conta
        passo("triado", "em_analise", "09:00:00"),
        passo("em_analise", "concluido", "09:12:30"),
    ]) == 750


def test_devolvido_e_repego_soma_os_dois_periodos():
    assert tempo_em_analise([
        passo("triado", "em_analise", "09:00:00"),
        passo("em_analise", "triado", "09:05:00"),   # 5 min
        passo("triado", "em_analise", "14:00:00"),   # o tempo na fila NAO conta
        passo("em_analise", "concluido", "14:10:00"),  # 10 min
    ]) == 900


def test_trilha_que_comeca_no_meio_nao_e_medida():
    """Um tempo parcial apresentado como total seria pior que nenhum."""
    assert tempo_em_analise([passo("em_analise", "concluido", "09:00:00")]) is None


def test_trilha_que_comeca_no_meio_e_repegada_depois_nao_e_medida():
    """O caso que distingue "nao medido" de "medido pela metade": o primeiro
    periodo (antes da trilha) e desconhecido; somar so o segundo devolveria
    5 min como se fosse o tempo total. Uma primeira versao deste arquivo so
    testava a trilha de um passo - e uma mutacao que media pela metade passou."""
    assert tempo_em_analise([
        passo("em_analise", "triado", "09:00:00"),     # saida sem entrada
        passo("triado", "em_analise", "10:00:00"),
        passo("em_analise", "concluido", "10:05:00"),
    ]) is None


def test_caso_ainda_nao_concluido_nao_e_medido():
    assert tempo_em_analise([
        passo("triado", "em_analise", "09:00:00"),
        passo("em_analise", "triado", "09:05:00"),
    ]) is None
    assert tempo_em_analise([passo("triado", "em_analise", "09:00:00")]) is None


def test_trilha_vazia():
    assert tempo_em_analise([]) is None
