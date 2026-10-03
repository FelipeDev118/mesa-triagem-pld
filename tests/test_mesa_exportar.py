"""Passo 1.8 - outputs/ vira uma vista do store.

O criterio: exportar do store tem que produzir os MESMOS numeros dos arquivos
commitados da entrega. Se a migracao mudasse qualquer valor, seria aqui que
apareceria.
"""
import csv
import json

import pytest
from tests_apoio import resposta_final

import agente
import confronto
from mesa import db, exportar, regras_run, triagem
from mesa.importar_cache import importar
from mesa.ingestao import ingerir

DADOS_REAIS = db.RAIZ / "dados" / "dados_nivel_2.json"
OUTPUTS_ENTREGA = db.RAIZ / "outputs"


@pytest.fixture
def store_triado(tmp_path, monkeypatch):
    """Store com os 30 alertas triados a partir do cache importado - nenhuma
    chamada de API, porque todos os pareceres ja existem."""
    caminho = tmp_path / "mesa.db"
    conn = db.conectar(caminho)
    ingerir(conn, DADOS_REAIS)
    regras_run.executar(conn)
    importar(conn)

    def nao_deve_chamar(**kwargs):
        raise AssertionError("chamou a API: o cache importado deveria cobrir os 30")

    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", nao_deve_chamar)
    triagem.triar(conn, pausa_s=0, verbose=False)
    monkeypatch.setattr(db, "CAMINHO_PADRAO", caminho)
    return conn


def test_export_reproduz_a_aderencia_da_entrega(store_triado, tmp_path):
    destino = tmp_path / "saida"
    r = exportar.exportar(store_triado, destino=destino)

    assert r["clientes"] == 30
    assert r["com_parecer"] == 30
    assert r["fundamentados"] == 28  # o numero do README

    with open(OUTPUTS_ENTREGA / "aderencia_pareceres.csv", encoding="utf-8") as f:
        da_entrega = {l["cliente_id"]: l["fundamentado"] == "True" for l in csv.DictReader(f)}
    with open(destino / "aderencia_pareceres.csv", encoding="utf-8") as f:
        do_store = {l["cliente_id"]: l["fundamentado"] == "True" for l in csv.DictReader(f)}
    assert do_store == da_entrega


def test_export_reproduz_os_niveis_de_risco_da_entrega(store_triado, tmp_path):
    destino = tmp_path / "saida"
    exportar.exportar(store_triado, destino=destino)

    do_store = {
        r["cliente_id"]: (r["parecer"] or {}).get("nivel_risco")
        for r in json.loads((destino / "pareceres_lote.json").read_text(encoding="utf-8"))
    }
    da_entrega = {
        r["cliente_id"]: (r["parecer"] or {}).get("nivel_risco")
        for r in json.loads(
            (OUTPUTS_ENTREGA / "pareceres_lote.json").read_text(encoding="utf-8"))
    }
    assert do_store == da_entrega


def test_export_gera_todos_os_artefatos(store_triado, tmp_path):
    destino = tmp_path / "saida"
    exportar.exportar(store_triado, destino=destino)
    esperados = {
        "pareceres_lote.json", "metricas_lote.csv", "aderencia_pareceres.csv",
        "chamadas_llm.csv", "custo_resumo.json",
    }
    assert {p.name for p in destino.iterdir()} == esperados


def test_export_e_regeneravel_sem_variacao(store_triado, tmp_path):
    """Descartavel e regeneravel: exportar duas vezes tem que dar o mesmo arquivo."""
    a, b = tmp_path / "a", tmp_path / "b"
    exportar.exportar(store_triado, destino=a)
    exportar.exportar(store_triado, destino=b)
    for nome in ["pareceres_lote.json", "metricas_lote.csv", "aderencia_pareceres.csv"]:
        assert (a / nome).read_bytes() == (b / nome).read_bytes(), nome


# ---------- confronto --store ----------


def test_confronto_do_store_reproduz_o_resumo_da_entrega(store_triado, tmp_path, monkeypatch):
    """<<< aceite do 1.8 >>> 76,67% / 23 concordantes / 7 divergentes."""
    monkeypatch.setattr(confronto, "OUTPUTS_DIR", tmp_path)
    confronto.main(usar_store=True)

    resumo = json.loads((tmp_path / "confronto_resumo.json").read_text(encoding="utf-8"))
    da_entrega = json.loads(
        (OUTPUTS_ENTREGA / "confronto_resumo.json").read_text(encoding="utf-8"))
    assert resumo == da_entrega
    assert resumo["taxa_concordancia_entre_validas"] == 0.7667
    assert (resumo["concordantes"], resumo["divergentes_qualitativas"]) == (23, 7)


def test_confronto_do_store_gera_o_mesmo_csv_da_entrega(store_triado, tmp_path, monkeypatch):
    monkeypatch.setattr(confronto, "OUTPUTS_DIR", tmp_path)
    confronto.main(usar_store=True)

    def ler(caminho):
        with open(caminho, encoding="utf-8") as f:
            return [
                {k: l[k] for k in ("cliente_id", "nivel_risco_esperado_regra",
                                   "nivel_risco_agente", "concorda")}
                for l in csv.DictReader(f)
            ]

    assert ler(tmp_path / "confronto_regra_vs_agente.csv") == \
        ler(OUTPUTS_ENTREGA / "confronto_regra_vs_agente.csv")


def test_confronto_sem_store_orienta(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "CAMINHO_PADRAO", tmp_path / "nao-existe.db")
    with pytest.raises(SystemExit, match="mesa.ingestao"):
        confronto.carregar_do_store()


def test_export_sem_custo_no_store_nao_apaga_medicao_existente(store_triado, tmp_path):
    """Regressao de um bug real, pego ao regerar outputs/ pela primeira vez.

    Uma triagem 100% reaproveitada do cache importado faz zero chamada de API -
    corretamente - e o cache antigo nunca guardou custo por chamada. O export
    escrevia "custo zero" por cima de chamadas_llm.csv e custo_resumo.json,
    apagando a medicao real da execucao original, que e irrecuperavel.
    """
    destino = tmp_path / "saida"
    destino.mkdir()
    (destino / "chamadas_llm.csv").write_text("medicao,real\n1,2\n", encoding="utf-8")
    (destino / "custo_resumo.json").write_text('{"custo_total_usd": 0.0123}', encoding="utf-8")

    assert store_triado.execute("SELECT COUNT(*) FROM chamadas_llm").fetchone()[0] == 0
    exportar.exportar(store_triado, destino=destino)

    assert (destino / "chamadas_llm.csv").read_text(encoding="utf-8") == "medicao,real\n1,2\n"
    assert json.loads((destino / "custo_resumo.json").read_text())["custo_total_usd"] == 0.0123


def test_export_com_custo_no_store_escreve_normalmente(store_triado, tmp_path):
    """O contrario tambem tem que valer: havendo medicao no store, ela e escrita."""
    store_triado.execute(
        "INSERT INTO chamadas_llm (cliente_id, turno, tipo_turno, modelo, tokens_entrada, "
        "tokens_saida, tokens_total, latencia_s, custo_usd, transporte, "
        "tentativas_rate_limit, registrado_em) VALUES "
        "('CLI-014', 1, 'resposta_final', 'gpt-oss', 100, 50, 150, 1.5, 0.00005, "
        "'import_direto', 0, '2026-09-12T00:00:00Z')"
    )
    store_triado.commit()

    destino = tmp_path / "saida"
    destino.mkdir()
    (destino / "custo_resumo.json").write_text('{"custo_total_usd": 999.0}', encoding="utf-8")
    exportar.exportar(store_triado, destino=destino)

    resumo = json.loads((destino / "custo_resumo.json").read_text(encoding="utf-8"))
    assert resumo["chamadas_api"] == 1
    assert resumo["custo_total_usd"] < 1.0
