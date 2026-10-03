"""Fase 6.0 - o cenario de isca PLANTADO, com gabarito. Vem ANTES do detector.

Sem gabarito, "o botao achou coisas" nao prova nada: na base sintetica, 57
contrapartes ja ligam cliente sinalizado a cliente sem sinal - por coincidencia
(nomes sorteados de uma lista curta). Um detector que "acha ligacoes" ali
sempre acha. A pergunta que importa e outra: ele acha as que foram PLANTADAS,
e quanto lixo traz junto? So da para responder sabendo de antemao quais sao.

O cenario e montado POR CIMA da base real (dados/dados_nivel_2.json): as
coincidencias dela entram como ruido de graca, o mais realista que existe aqui.

    python -m mesa.cenario_isca --semente 7 --pasta outputs/cenario_isca
    # -> outputs/cenario_isca/mesa.db (store pronto para a API), os arquivos de
    #    cada lote e gabarito.json

O que e plantado (tudo deterministico pela semente):

  a ISCA      cliente novo, chamativo: operacoes comuns e UMA enorme (pega pela
              regra de valor atipico). E o caminhao dos 30 kg.
  padrao A    fracionamento DISTRIBUIDO: uma operacao em cada um de 5 a 8
              clientes novos, logo abaixo do limite individual do
              fracionamento, com uma contraparte rara que a isca tambem usa,
              poucos dias antes/depois da operacao da isca com ela.
  padrao B    volume grande DURANTE A ANALISE: um lote que chega enquanto a isca
              esta em_analise, com operacoes de valor ALTO em clientes novos,
              ligadas a isca por outra contraparte rara. Os clientes sao
              empresas grandes (100 mil e normal para elas) - por isso nenhuma
              regra dispara.
  RUIDO       (1) contraparte POPULAR da base, dentro da janela e na faixa
              abaixo do limite - parece fracionamento, mas a contraparte e de
              todo mundo; (2) contraparte rara da isca, mas com valores altos
              FORA da janela de dias; (3) a contraparte do padrao B num lote que
              chega DEPOIS que a isca saiu de analise.

Os horarios de ingestao e da trilha sao FABRICADOS (fixos, conhecidos): e o que
deixa testar "entrou durante a analise" sem depender do relogio. So este modulo
faz isso, e so em store de cenario - nunca no store real (`montar` recusa
caminho que ja exista).
"""
import argparse
import json
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import mesa  # noqa: F401  - poe nivel_2/ no sys.path
from dados import DADOS_PATH, PARAMETROS_REGRAS
from mesa import db, regras_run
from mesa.ingestao import ingerir

ISCA = "CLI-101"
ANALISTA_CENARIO = "cenario:analista"

# Horarios fabricados (UTC). A ordem e o que importa: o padrao B chega DENTRO
# do periodo em analise, o ruido (3) chega DEPOIS de a isca ser devolvida.
HORARIOS = {
    "lote_base": "2026-06-02T08:00:00Z",
    "lote_cenario": "2026-06-02T08:30:00Z",
    "isca_pega": "2026-06-02T09:00:00Z",
    "lote_durante_analise": "2026-06-02T10:00:00Z",
    "isca_devolvida": "2026-06-02T11:00:00Z",
    "lote_depois_da_analise": "2026-06-02T12:00:00Z",
}

# Nomes que NAO existem na base (conferido em gerar()). Primeiro nome + sufixo.
_NOMES = ["Vereda", "Atalaia", "Cobalto", "Jatoba", "Quartzo", "Ravena", "Sertao",
          "Itaoca", "Barlavento", "Carnauba", "Tabatinga", "Urucum"]
_SUFIXOS = ["Logistica LTDA", "Cargas ME", "Armazens SA", "Fomento LTDA"]
# A contraparte mais usada da base (7 clientes, de marco a maio): o "ruido
# popular". Conferido em gerar() - se a base mudar, o cenario avisa.
CONTRAPARTE_POPULAR = "Aurora Servicos SA"

TIPOS = ["pagamento", "transferencia_enviada", "transferencia_recebida", "deposito"]
CANAIS = ["pix", "ted", "boleto"]


@dataclass
class Cenario:
    semente: int
    lotes: dict[str, list[dict]]      # nome do lote -> operacoes (formato do JSON de entrada)
    gabarito: dict
    taxa: float = 5.4
    contrapartes_base: list[str] = field(default_factory=list)


def _base() -> dict:
    return json.loads(Path(DADOS_PATH).read_text(encoding="utf-8"))


def gerar(semente: int) -> Cenario:
    """Monta as operacoes de cada lote e o gabarito. Nao toca banco nenhum."""
    rnd = random.Random(semente)
    base = _base()
    ops_base = base["operacoes"]
    nomes_base = {o["contraparte"] for o in ops_base}
    contrapartes_base = sorted(nomes_base)
    clientes_popular = {o["cliente_id"] for o in ops_base if o["contraparte"] == CONTRAPARTE_POPULAR}
    if len(clientes_popular) < 5:
        raise RuntimeError(f"'{CONTRAPARTE_POPULAR}' deixou de ser popular na base - revise o cenario")

    sorteados = rnd.sample([f"{n} {s}" for n in _NOMES for s in _SUFIXOS], 3)
    assert not nomes_base.intersection(sorteados), "nome plantado colide com a base"
    cp_a, cp_b, cp_fora = sorteados

    limite = PARAMETROS_REGRAS["frac_max_individual"]
    seq = iter(range(1, 10_000))

    def nova_op(cliente, dia: date, valor, contraparte, **kw):
        return {
            "id": f"OPX-{semente:03d}-{next(seq):04d}", "cliente_id": cliente,
            "data": dia.isoformat(), "valor": round(valor, 2), "moeda": "BRL",
            "canal": kw.get("canal", rnd.choice(CANAIS)), "tipo": kw.get("tipo", rnd.choice(TIPOS)),
            "contraparte": contraparte, "observacao": "",
        }

    def cobertura(cliente, n, faixa, evitar: set[date], inicio=date(2026, 3, 1), dias=88):
        """Operacoes comuns, com contrapartes REAIS da base (o que liga o cliente
        a outros por coincidencia, como na base), uma por dia no maximo - assim
        nenhum dia junta 3 operacoes e o fracionamento nao dispara."""
        livres = [inicio + timedelta(d) for d in range(dias)]
        livres = [d for d in livres if d not in evitar]
        return [nova_op(cliente, d, rnd.uniform(*faixa), rnd.choice(contrapartes_base))
                for d in sorted(rnd.sample(livres, n))]

    # --- a isca: comum + UMA operacao enorme (dispara valor atipico) ---
    dia_isca = date(2026, 4, 1) + timedelta(rnd.randrange(30))
    dia_fora = dia_isca + timedelta(40)   # bem fora de qualquer janela de dias
    isca = cobertura(ISCA, 7, (2_000, 8_000), {dia_isca, dia_fora})
    isca += [
        nova_op(ISCA, dia_isca, rnd.uniform(140_000, 220_000), cp_a, tipo="transferencia_enviada"),
        nova_op(ISCA, dia_isca - timedelta(rnd.randrange(1, 4)), rnd.uniform(3_000, 6_000), cp_b),
        nova_op(ISCA, dia_isca + timedelta(rnd.randrange(1, 4)), rnd.uniform(3_000, 6_000),
                CONTRAPARTE_POPULAR),
        nova_op(ISCA, dia_isca + timedelta(rnd.randrange(4, 8)), rnd.uniform(3_000, 6_000), cp_fora),
    ]

    # --- padrao A: uma operacao por cliente, logo abaixo do limite, na janela ---
    plantadas_a, lote_cenario = [], list(isca)
    for i in range(rnd.randint(5, 8)):
        cliente = f"CLI-{111 + i}"
        dia = dia_isca + timedelta(rnd.randint(-5, 5))
        op = nova_op(cliente, dia, rnd.uniform(0.86, 0.99) * limite, cp_a)
        plantadas_a.append(op["id"])
        # mediana da cobertura acima de 4 mil: 5x a mediana fica acima do
        # limite individual, e a operacao plantada nao vira "valor atipico"
        lote_cenario += [op, *cobertura(cliente, rnd.randint(4, 6), (5_000, 12_000), {dia})]

    # --- ruido (1): contraparte popular, na janela, na faixa abaixo do limite ---
    ruido_popular = []
    for i in range(3):
        cliente = f"CLI-{131 + i}"
        dia = dia_isca + timedelta(rnd.randint(-5, 5))
        op = nova_op(cliente, dia, rnd.uniform(0.86, 0.99) * limite, CONTRAPARTE_POPULAR)
        ruido_popular.append(op["id"])
        lote_cenario += [op, *cobertura(cliente, rnd.randint(4, 6), (5_000, 12_000), {dia})]

    # --- ruido (2): contraparte rara da isca, valores altos FORA da janela ---
    ruido_fora = []
    for i in range(2):
        cliente = f"CLI-{136 + i}"
        op = nova_op(cliente, dia_fora + timedelta(i), rnd.uniform(0.86, 0.99) * limite, cp_fora)
        ruido_fora.append(op["id"])
        lote_cenario += [op, *cobertura(cliente, rnd.randint(4, 6), (5_000, 12_000), {dia_fora + timedelta(i)})]

    # --- padrao B: chega DURANTE a analise; empresas grandes, valor alto ---
    dia_b = date(2026, 6, 1)
    plantadas_b, lote_durante = [], []
    for i in range(3):
        cliente = f"CLI-{121 + i}"
        op = nova_op(cliente, dia_b + timedelta(i % 2), rnd.uniform(80_000, 160_000), cp_b)
        plantadas_b.append(op["id"])
        lote_durante += [op, *cobertura(cliente, rnd.randint(4, 6), (40_000, 90_000), set(),
                                        inicio=date(2026, 3, 1), dias=80)]

    # --- ruido (3): mesma contraparte do B, mas chega DEPOIS da analise ---
    ruido_depois = []
    cliente = "CLI-139"
    op = nova_op(cliente, dia_b + timedelta(1), rnd.uniform(80_000, 160_000), cp_b)
    ruido_depois.append(op["id"])
    lote_depois = [op, *cobertura(cliente, 5, (40_000, 90_000), set(), dias=80)]

    gabarito = {
        "semente": semente,
        "isca": ISCA,
        "dia_isca": dia_isca.isoformat(),
        "contrapartes": {"padrao_a": cp_a, "padrao_b": cp_b, "fora_da_janela": cp_fora,
                         "popular": CONTRAPARTE_POPULAR},
        "padrao_a": plantadas_a,
        "padrao_b": plantadas_b,
        "ruido": {"popular_na_janela": ruido_popular, "fora_da_janela": ruido_fora,
                  "lote_depois_da_analise": ruido_depois},
        "horarios": HORARIOS,
    }
    return Cenario(
        semente=semente,
        lotes={"lote_cenario": lote_cenario, "lote_durante_analise": lote_durante,
               "lote_depois_da_analise": lote_depois},
        gabarito=gabarito,
        taxa=base["taxa_cambio_usd_brl"],
        contrapartes_base=contrapartes_base,
    )


def _escrever_lote(pasta: Path, nome: str, operacoes: list[dict], taxa: float) -> Path:
    caminho = pasta / f"{nome}.json"
    caminho.write_text(json.dumps({"taxa_cambio_usd_brl": taxa, "operacoes": operacoes},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    return caminho


def _ingerir_em(conn: sqlite3.Connection, caminho: Path, quando: str) -> int:
    lote = ingerir(conn, caminho).lote_id
    # horario FABRICADO - so aqui, em store de cenario (ver docstring do modulo)
    conn.execute("UPDATE lotes_ingestao SET ingerido_em = ? WHERE id = ?", (quando, lote))
    regras_run.executar(conn)
    conn.commit()
    return lote


def _transicao(conn: sqlite3.Connection, alerta_id: int, de: str, para: str, quando: str) -> None:
    dono = ANALISTA_CENARIO if para == "em_analise" else None
    with conn:
        cur = conn.execute("UPDATE alertas SET estado = ?, analista_id = ? WHERE id = ? AND estado = ?",
                           (para, dono, alerta_id, de))
        assert cur.rowcount == 1, f"alerta {alerta_id} nao estava '{de}'"
        conn.execute(
            "INSERT INTO transicoes (alerta_id, estado_anterior, estado_novo, ator, ator_tipo, "
            "registrado_em) VALUES (?, ?, ?, ?, 'analista', ?)",
            (alerta_id, de, para, ANALISTA_CENARIO, quando),
        )


def alerta_da_isca(conn: sqlite3.Connection) -> int:
    from mesa.repositorio import VIGENTE
    return conn.execute(
        f"SELECT a.id FROM alertas a WHERE a.cliente_id = ? AND {VIGENTE}", (ISCA,)
    ).fetchone()[0]


def montar(pasta: Path, semente: int) -> tuple[Path, dict]:
    """Cria `pasta`/mesa.db com a base real + o cenario, na ordem fabricada:

      lote base -> regras -> lote do cenario (isca, A, ruidos 1 e 2) -> regras
      -> isca PEGA -> lote durante a analise (B) -> regras -> isca DEVOLVIDA
      -> lote depois da analise (ruido 3) -> regras

    Grava os arquivos de cada lote e gabarito.json (com o alerta da isca) ao
    lado do banco. Recusa uma pasta que ja tenha banco: nunca escreve por cima.
    """
    pasta = Path(pasta)
    caminho = pasta / "mesa.db"
    if caminho.exists():
        raise FileExistsError(f"{caminho} ja existe - o cenario nao escreve por cima de banco nenhum")
    pasta.mkdir(parents=True, exist_ok=True)
    cenario = gerar(semente)
    arquivos = {n: _escrever_lote(pasta, n, ops, cenario.taxa) for n, ops in cenario.lotes.items()}

    conn = db.conectar(caminho)
    try:
        _ingerir_em(conn, Path(DADOS_PATH), HORARIOS["lote_base"])
        _ingerir_em(conn, arquivos["lote_cenario"], HORARIOS["lote_cenario"])
        isca = alerta_da_isca(conn)
        _transicao(conn, isca, "novo", "em_analise", HORARIOS["isca_pega"])
        _ingerir_em(conn, arquivos["lote_durante_analise"], HORARIOS["lote_durante_analise"])
        _transicao(conn, isca, "em_analise", "triado", HORARIOS["isca_devolvida"])
        _ingerir_em(conn, arquivos["lote_depois_da_analise"], HORARIOS["lote_depois_da_analise"])
        assert alerta_da_isca(conn) == isca, "um lote posterior substituiu a isca"
    finally:
        conn.close()

    gabarito = {**cenario.gabarito, "alerta_isca": isca}
    (pasta / "gabarito.json").write_text(json.dumps(gabarito, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    return caminho, gabarito


def medir_semente(pasta: Path, semente: int) -> dict:
    """6.2 - monta o cenario da semente e confere a caca contra o gabarito.

    "topo" = as |A| + |B| primeiras ligacoes (a meta esta no ROADMAP, gravada
    ANTES da primeira medicao)."""
    from mesa import contra_isca

    caminho, g = montar(Path(pasta) / f"semente_{semente}", semente)
    conn = db.abrir_para_leitura(caminho)
    try:
        resultado = contra_isca.cacar(conn, g["alerta_isca"])["ligacoes"]
    finally:
        conn.close()
    ligacoes = [l["operacao_id"] for l in resultado]
    a, b = set(g["padrao_a"]), set(g["padrao_b"])
    plantada = [l["escore"] for l in resultado if l["operacao_id"] in a | b]
    lixo = [l["escore"] for l in resultado if l["operacao_id"] not in a | b]
    topo = set(ligacoes[:len(a) + len(b)])
    return {
        "semente": semente,
        "plantadas_a": len(a), "plantadas_b": len(b), "ligacoes": len(ligacoes),
        "cobertura_a": len(a & topo) / len(a),
        "cobertura_b": len(b & topo) / len(b),
        "precisao_topo": len((a | b) & topo) / len(topo),
        # folga: pior plantada / melhor nao plantada. Cobertura de 100% com
        # margem 1,05 e sorte; com margem 3 e separacao
        "margem": (min(plantada) / max(lixo)) if plantada and lixo else None,
        "popular_no_topo": len(set(g["ruido"]["popular_na_janela"]) & topo),
        "fora_da_janela_na_lista": len(set(g["ruido"]["fora_da_janela"]) & set(ligacoes)),
        "depois_da_analise_na_lista": len(set(g["ruido"]["lote_depois_da_analise"]) & set(ligacoes)),
    }


def _faixa_de_sementes(texto: str) -> list[int]:
    inicio, _, fim = texto.partition("-")
    return list(range(int(inicio), int(fim or inicio) + 1))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Monta um store com o cenario de isca plantado.")
    p.add_argument("--semente", type=int, default=7)
    p.add_argument("--pasta", type=Path, default=db.RAIZ / "outputs" / "cenario_isca")
    p.add_argument("--medir", metavar="INICIO-FIM",
                   help="6.2: mede a caca nas sementes da faixa (cada uma numa subpasta de --pasta)")
    args = p.parse_args(argv)
    if args.medir:
        linhas = [medir_semente(args.pasta, s) for s in _faixa_de_sementes(args.medir)]
        for l in linhas:
            print(f"semente {l['semente']:>3}: A {l['cobertura_a']:.0%} ({l['plantadas_a']}) | "
                  f"B {l['cobertura_b']:.0%} | precisao {l['precisao_topo']:.0%} | "
                  f"margem {l['margem']:.2f} | ligacoes {l['ligacoes']} | ruido: popular no topo {l['popular_no_topo']}, "
                  f"fora da janela {l['fora_da_janela_na_lista']}, "
                  f"depois da analise {l['depois_da_analise_na_lista']}")
        media = lambda k: sum(l[k] for l in linhas) / len(linhas)
        print(f"MEDIA: A {media('cobertura_a'):.1%} (min {min(l['cobertura_a'] for l in linhas):.0%}) | "
              f"B {media('cobertura_b'):.1%} (min {min(l['cobertura_b'] for l in linhas):.0%}) | "
              f"precisao {media('precisao_topo'):.1%} | margem min "
              f"{min(l['margem'] for l in linhas):.2f} | ligacoes {media('ligacoes'):.1f}")
        return 0
    caminho, gabarito = montar(args.pasta, args.semente)
    print(f"cenario (semente {args.semente}) em {caminho} | isca {ISCA} = alerta "
          f"{gabarito['alerta_isca']} | plantadas: {len(gabarito['padrao_a'])} (A), "
          f"{len(gabarito['padrao_b'])} (B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
