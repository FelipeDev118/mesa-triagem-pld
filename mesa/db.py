"""Conexao e esquema do store da Mesa de Triagem.

Por que SQLite e nao "so continuar com os JSONs": as ferramentas do agente
reliam a base inteira para responder sobre um cliente, e nada do que o pipeline
produzia sobrevivia a um `rm outputs/`. O store resolve as duas coisas de uma
vez - indice por cliente/data, e persistencia com trilha de auditoria.

Por que SQLite e nao Postgres agora: o volume desta base (317 operacoes) nao
justifica um servico, e todo o DDL em esquema.sql foi escrito portavel de
proposito. A troca fica sendo uma edicao localizada, nao uma reescrita.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CAMINHO_PADRAO = RAIZ / "outputs" / "mesa.db"
ESQUEMA_SQL = Path(__file__).resolve().parent / "esquema.sql"

# Versao do esquema aplicado, gravada em PRAGMA user_version. Serve para detectar
# um banco velho antes de escrever nele, em vez de descobrir na primeira query
# que uma coluna nao existe. Bump manual a cada alteracao de esquema.
#
# Ate a v5, alterar o esquema significava apagar o banco e reingerir: o store so
# continha dado sintetico reprocessavel. A v6 (Fase 4) grava DECISAO DE ANALISTA,
# que nao se reprocessa - dai em diante o banco e migrado, nao reconstruido.
VERSAO_ESQUEMA = 8

# Como levar um banco da versao N para N+1. Cada entrada e o que o DDL completo
# (esquema.sql, todo IF NOT EXISTS) NAO consegue fazer sozinho num banco que ja
# existe: `CREATE TABLE IF NOT EXISTS` nao altera tabela existente, entao coluna
# nova em tabela velha precisa de ALTER. Tabela, indice e trigger novos vem do
# DDL completo, aplicado logo depois.
#
# Uma versao que nao esta aqui nao tem migracao, e o banco e recusado - carimbar
# a versao nova sem as colunas faria o codigo ler o que nao existe.
#
# O teste que sustenta isto: banco v5, v6 e v7 (DDL congelado em tests/esquemas/)
# migrados para a versao atual tem a MESMA estrutura de um banco criado agora.
MIGRACOES: dict[int, list[str]] = {
    5: [],  # 5 -> 6: so tabelas novas (transicoes, decisoes)
    6: [    # 6 -> 7: substituicao de alertas e escopo da execucao (Fase 5)
        "ALTER TABLE execucoes_regras ADD COLUMN escopo TEXT NOT NULL DEFAULT 'completa' "
        "CHECK (escopo IN ('completa', 'incremental'))",
        "ALTER TABLE alertas ADD COLUMN substitui_alerta_id INTEGER REFERENCES alertas(id)",
    ],
    7: [],  # 7 -> 8: so tabela nova (suspeitas, Fase 6)
}


def agora_utc() -> str:
    """Timestamp ISO-8601 em UTC, o formato usado em todas as colunas *_em.

    UTC e nao horario local porque um registro de decisao de risco que muda de
    significado conforme o fuso de quem gravou nao serve de trilha de auditoria.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def conectar(caminho: Path | str | None = None, criar_esquema: bool = True,
             check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre (e cria, se preciso) o banco com os PRAGMAs que o resto do codigo assume.

    `foreign_keys=ON` nao e opcional aqui: o SQLite deixa as FKs DESLIGADAS por
    padrao, por compatibilidade historica, e por conexao - declarar REFERENCES no
    DDL sem ligar o PRAGMA e ter integridade referencial so no comentario. Como e
    por conexao, tem que ser feito aqui e nao no esquema.

    `check_same_thread=False` e para a API (mesa/api.py), e so para ela. O
    FastAPI abre a conexao numa thread do pool, roda o endpoint em outra e fecha
    numa terceira - e o sqlite3 recusa, por padrao, usar a conexao fora da thread
    que a criou. Medido, nao suposto: 278 de 300 requisicoes concorrentes deram
    500 com o padrao. Desligar a checagem e seguro ali porque cada requisicao tem
    a PROPRIA conexao, que troca de thread mas nunca e usada por duas ao mesmo
    tempo. Compartilhar uma conexao entre requisicoes continuaria sendo erro.
    """
    # None = CAMINHO_PADRAO lido AGORA. Com `caminho=CAMINHO_PADRAO` na
    # assinatura, o padrao era fixado na importacao do modulo, e trocar
    # db.CAMINHO_PADRAO depois nao valia para quem chama conectar() sem
    # argumento - numa auditoria, um script que "apontava para um banco
    # temporario" escreveu no store real. Mesmo criterio que a API ja seguia.
    caminho = Path(CAMINHO_PADRAO if caminho is None else caminho)
    em_memoria = str(caminho) == ":memory:"
    if not em_memoria:
        caminho.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(caminho, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not em_memoria:
        # WAL: leitor nao bloqueia escritor. Importa quando o worker de triagem
        # (Fase 1.7) estiver gravando parecer enquanto a API le a fila.
        # Nao se aplica a bancos em memoria - por isso o guard, e nao um try/except
        # que engoliria um erro real de disco.
        conn.execute("PRAGMA journal_mode = WAL")

    if criar_esquema:
        aplicar_esquema(conn)
    return conn


def abrir_para_leitura(caminho: Path | str | None = None) -> sqlite3.Connection:
    """Abre o store SEM aplicar o esquema, e recusa versao diferente.

    Para quem so LE (verificar_ambiente --store, confronto --store). `conectar()`
    migra o banco ao abrir - num comando de verificacao isso e escrita
    escondida: rodar a verificacao ja migrou o store real enquanto um servidor
    de codigo anterior lia dele, e o servidor passou a responder 503. Medido,
    nao suposto (Fase 5). Mesmo criterio da API."""
    conn = conectar(caminho, criar_esquema=False)
    versao = conn.execute("PRAGMA user_version").fetchone()[0]
    if versao != VERSAO_ESQUEMA:
        conn.close()
        raise RuntimeError(
            f"store na versao de esquema {versao}, este codigo espera {VERSAO_ESQUEMA} "
            "- rode: python -m mesa.db"
        )
    return conn


def aplicar_esquema(conn: sqlite3.Connection) -> None:
    """Cria o esquema num banco novo, migra um banco antigo, e nao faz nada num
    banco ja atualizado (todo o DDL usa IF NOT EXISTS). E o que permite chamar
    `conectar()` sem saber se o banco existe.

    A migracao e UMA transacao: os ALTER de cada versao, o DDL completo e o
    carimbo da versao. Falhou no meio, o banco fica como estava - nunca com
    metade das colunas e a versao nova.

    Recusa: banco mais novo que o codigo, e banco antigo sem caminho em MIGRACOES."""
    versao_atual = conn.execute("PRAGMA user_version").fetchone()[0]
    if versao_atual > VERSAO_ESQUEMA:
        raise RuntimeError(
            f"banco na versao de esquema {versao_atual}, codigo espera {VERSAO_ESQUEMA} "
            "- este codigo e mais antigo que o banco; atualize o codigo em vez de "
            "escrever num esquema que ele nao entende"
        )
    # 0 = banco recem-criado (ou vazio): o DDL inteiro cria tudo do zero
    passos: list[str] = []
    if versao_atual not in (0, VERSAO_ESQUEMA):
        for v in range(versao_atual, VERSAO_ESQUEMA):
            if v not in MIGRACOES:
                raise RuntimeError(
                    f"banco na versao de esquema {versao_atual}, codigo espera "
                    f"{VERSAO_ESQUEMA}, e nao ha migracao a partir da v{v}. Reconstrua o "
                    "store (python -m mesa.ingestao && python -m mesa.regras_run && ...)"
                )
            passos.extend(MIGRACOES[v])

    script = "\n".join([
        "BEGIN;",
        *(f"{sql};" for sql in passos),
        ESQUEMA_SQL.read_text(encoding="utf-8"),
        f"PRAGMA user_version = {VERSAO_ESQUEMA};",
        "COMMIT;",
    ])
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


if __name__ == "__main__":
    # `python -m mesa.db` - cria o store, ou migra um de versao aditiva. E o
    # comando que a API aponta quando recusa um store de versao anterior.
    import sys

    caminho = Path(sys.argv[1]) if len(sys.argv) > 1 else CAMINHO_PADRAO
    existia = caminho.exists()
    antes = None
    if existia:
        bruta = sqlite3.connect(caminho)
        antes = bruta.execute("PRAGMA user_version").fetchone()[0]
        bruta.close()
    conn = conectar(caminho)
    conn.close()
    if not existia:
        print(f"store criado em {caminho} (esquema v{VERSAO_ESQUEMA})")
    elif antes == VERSAO_ESQUEMA:
        print(f"{caminho} ja esta no esquema v{VERSAO_ESQUEMA} - nada a fazer")
    else:
        print(f"{caminho} migrado: esquema v{antes} -> v{VERSAO_ESQUEMA}")
