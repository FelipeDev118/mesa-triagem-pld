"""Passo 0.2/0.3 - carrega um arquivo de operacoes para o store.

Decisao central deste modulo: a ingestao NAO reimplementa a limpeza. Ela chama
`carregar_e_limpar()` de nivel_2/dados.py - a mesma funcao coberta pelos 20
testes de limite em tests/test_dados.py - e apenas persiste o resultado. Escrever
um segundo parser de data ou uma segunda conversao de moeda aqui criaria duas
verdades sobre o que e "a base tratada", e a que ninguem olha e a que diverge.

Isso tambem e o que torna o passo 0.4 (o DataFrame lido do store tem que ser
igual ao lido do JSON) uma consequencia do desenho, e nao uma coincidencia a
ser torcida.
"""
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import mesa  # noqa: F401  - poe nivel_2/ no sys.path
from dados import DADOS_PATH, carregar_e_limpar
from mesa.db import agora_utc, conectar

COLUNAS_INSERT = (
    "id, lote_id, cliente_id, data, data_valida, valor, moeda, valor_brl, "
    "canal, tipo, contraparte, observacao"
)

# O que define se uma operacao que ja existe foi CORRIGIDA: os campos como
# chegaram no arquivo. valor_brl fica de fora de proposito - e derivado com a
# taxa do lote, e o mesmo arquivo reenviado com outra taxa nao e correcao de
# nada (a taxa e propriedade do lote, Fase 0).
CAMPOS_BRUTOS = ("cliente_id", "data", "data_valida", "valor", "moeda", "canal",
                 "tipo", "contraparte", "observacao")


@dataclass
class ResultadoIngestao:
    lote_id: int
    origem: str
    operacoes_brutas: int
    operacoes_inseridas: int
    duplicatas_ignoradas: int
    operacoes_corrigidas: int = 0
    # clientes com operacao nova ou corrigida neste lote (antes E depois da
    # correcao, se ela mudou o cliente da operacao) - o delta da Fase 5
    clientes_afetados: frozenset[str] = frozenset()

    def __str__(self) -> str:
        return (
            f"lote {self.lote_id} ({self.origem}): {self.operacoes_brutas} brutas, "
            f"{self.operacoes_inseridas} inseridas, "
            f"{self.operacoes_corrigidas} corrigidas, "
            f"{self.duplicatas_ignoradas} ignoradas | "
            f"{len(self.clientes_afetados)} clientes afetados"
        )


def _sha256(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


def ingerir(conn: sqlite3.Connection, caminho: Path | str = DADOS_PATH) -> ResultadoIngestao:
    """Le o arquivo, aplica a limpeza da entrega e grava operacoes + o lote.

    Cada operacao do arquivo cai em um de tres casos:
      - id novo              -> inserida
      - id existente, campos brutos iguais -> ignorada (reenvio)
      - id existente, algum campo bruto diferente -> CORRIGIDA (Fase 5.2): o
        UPDATE grava a versao nova com este lote, e o trigger do schema guarda
        a anterior em operacoes_historico. A ingestao nao precisa lembrar disso.

    `duplicatas_ignoradas` conta o que estava no arquivo e nao mudou nada: a
    duplicata interna ao arquivo (as 5 que a limpeza remove por id) e o reenvio
    de operacao ja conhecida.
    """
    caminho = Path(caminho)
    df, taxa = carregar_e_limpar(caminho)

    # As contagens BRUTAS vem do arquivo, nao do DataFrame ja deduplicado - e a
    # unica chance de registra-las, porque a tabela operacoes so guarda o que
    # sobreviveu a limpeza. Ex.: o arquivo tem 7 datas nulas, mas uma delas esta
    # numa das 5 duplicatas removidas, entao operacoes tem 6.
    with open(caminho, encoding="utf-8") as f:
        brutas = json.load(f)["operacoes"]
    operacoes_brutas = len(brutas)
    datas_nulas_brutas = sum(1 for o in brutas if not o.get("data"))
    operacoes_usd_brutas = sum(1 for o in brutas if o.get("moeda") == "USD")

    cur = conn.execute(
        "INSERT INTO lotes_ingestao (origem, sha256_arquivo, taxa_cambio_usd_brl, "
        "operacoes_brutas, operacoes_inseridas, duplicatas_ignoradas, "
        "datas_nulas_brutas, operacoes_usd_brutas, ingerido_em) "
        "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (str(caminho), _sha256(caminho), taxa, operacoes_brutas,
         datas_nulas_brutas, operacoes_usd_brutas, agora_utc()),
    )
    lote_id = cur.lastrowid

    linhas = [
        (
            row["id"],
            lote_id,
            row["cliente_id"],
            row["data"].strftime("%Y-%m-%d") if row["data_valida"] else None,
            int(bool(row["data_valida"])),
            float(row["valor"]),
            row["moeda"],
            float(row["valor_brl"]),
            row["canal"],
            row["tipo"],
            row["contraparte"],
            row["observacao"] or "",
        )
        for _, row in df.iterrows()
    ]

    existentes = {
        r["id"]: r for r in conn.execute(f"SELECT id, {', '.join(CAMPOS_BRUTOS)} FROM operacoes")
    }
    novas, correcoes, afetados = [], [], set()
    for linha in linhas:
        atual = existentes.get(linha[0])
        # linha = (id, lote_id, cliente_id, data, data_valida, valor, moeda,
        #          valor_brl, canal, tipo, contraparte, observacao)
        brutos = (linha[2], linha[3], linha[4], linha[5], linha[6], *linha[8:])
        if atual is None:
            novas.append(linha)
            afetados.add(linha[2])
        elif tuple(atual[c] for c in CAMPOS_BRUTOS) != brutos:
            correcoes.append(linha)
            afetados.update({linha[2], atual["cliente_id"]})

    conn.executemany(
        f"INSERT INTO operacoes ({COLUNAS_INSERT}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        novas,
    )
    # a versao anterior vai para operacoes_historico pelo trigger do schema
    conn.executemany(
        "UPDATE operacoes SET lote_id = ?, cliente_id = ?, data = ?, data_valida = ?, "
        "valor = ?, moeda = ?, valor_brl = ?, canal = ?, tipo = ?, contraparte = ?, "
        "observacao = ? WHERE id = ?",
        [(*l[1:], l[0]) for l in correcoes],
    )
    ignoradas = operacoes_brutas - len(novas) - len(correcoes)

    conn.execute(
        "UPDATE lotes_ingestao SET operacoes_inseridas = ?, duplicatas_ignoradas = ? "
        "WHERE id = ?",
        (len(novas), ignoradas, lote_id),
    )
    conn.commit()

    return ResultadoIngestao(
        lote_id=lote_id,
        origem=str(caminho),
        operacoes_brutas=operacoes_brutas,
        operacoes_inseridas=len(novas),
        duplicatas_ignoradas=ignoradas,
        operacoes_corrigidas=len(correcoes),
        clientes_afetados=frozenset(afetados),
    )


if __name__ == "__main__":
    caminho = Path(sys.argv[1]) if len(sys.argv) > 1 else DADOS_PATH
    with conectar() as conn:
        print(ingerir(conn, caminho))
