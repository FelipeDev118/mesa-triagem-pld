"""Fase 5.3 - o ciclo que roda sozinho: ingere o que chegou, reavalia o delta,
tria o que ficou novo.

    python -m mesa.ciclo --entrada dados/entrada              # uma passada
    python -m mesa.ciclo --entrada dados/entrada --a-cada 60  # em laco, a cada 60 min

Cada passo ja era idempotente por conta propria; o ciclo so os encadeia:
  1. arquivos da pasta ainda nao ingeridos - pelo sha256 do CONTEUDO, nao pelo
     nome: renomear um arquivo ja ingerido nao o faz entrar de novo, e um
     arquivo corrigido com o mesmo nome entra (e as correcoes viram versao, 5.2)
  2. regras sobre o delta (5.1) - sem lote novo, nenhuma execucao
  3. triagem dos alertas vigentes em 'novo' - so o delta paga chamada de LLM

Uma passada sem arquivo novo nao grava nada. E por isso que agendar (cron,
systemd, o laco --a-cada) e seguro: a frequencia so muda a latencia, nao o
resultado.

Dois ciclos ao mesmo tempo (um cron que atrasou, alguem rodando na mao) sao
barrados por uma trava de arquivo ao lado do banco. O segundo sai sem fazer
nada - e diz por que.

NAO exporta outputs/: aqueles arquivos sao os da entrega, commitados. Um ciclo
agendado sobrescrevendo-os a cada hora apagaria a evidencia versionada.
"""
import argparse
import fcntl
import hashlib
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from mesa import db, regras_run, triagem
from mesa.ingestao import ingerir


@dataclass
class ResultadoCiclo:
    executado: bool = True               # False = outro ciclo estava rodando
    arquivos_ingeridos: list[str] = field(default_factory=list)
    arquivos_ja_conhecidos: int = 0
    execucao: str | None = None          # "3 (incremental)" ou None
    triados: int = 0
    chamadas_api: int = 0

    def __str__(self) -> str:
        if not self.executado:
            return "outro ciclo em andamento - nada feito"
        return (
            f"{len(self.arquivos_ingeridos)} arquivo(s) novo(s), "
            f"{self.arquivos_ja_conhecidos} ja conhecido(s) | "
            f"regras: {self.execucao or 'nada a fazer'} | "
            f"triagem: {self.triados} alerta(s), {self.chamadas_api} chamada(s) de API"
        )


class CicloEmAndamento(Exception):
    pass


@contextmanager
def _trava(caminho_banco: Path):
    """Trava EXCLUSIVA e nao bloqueante num arquivo ao lado do banco.

    flock e liberada pelo sistema quando o processo morre - um ciclo que caiu
    no meio (kill -9, container reiniciado) nao deixa a trava presa para sempre,
    que e o defeito classico de "arquivo .lock que alguem esqueceu de apagar"."""
    caminho = caminho_banco.with_name(caminho_banco.name + ".ciclo.lock")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w") as arquivo:
        try:
            fcntl.flock(arquivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CicloEmAndamento()
        try:
            yield
        finally:
            fcntl.flock(arquivo, fcntl.LOCK_UN)


def _ja_ingeridos(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT sha256_arquivo FROM lotes_ingestao")}


def rodar(entrada: Path, caminho_banco: Path | None = None, pausa_s: float = triagem.PAUSA_RATE_LIMIT_S,
          verbose: bool = True) -> ResultadoCiclo:
    caminho_banco = Path(caminho_banco or db.CAMINHO_PADRAO)
    entrada = Path(entrada)
    entrada.mkdir(parents=True, exist_ok=True)
    resultado = ResultadoCiclo()
    try:
        with _trava(caminho_banco):
            conn = db.conectar(caminho_banco)
            try:
                conhecidos = _ja_ingeridos(conn)
                # ordem do nome: quem gera os arquivos controla a ordem (ex.:
                # prefixo de data), e uma correcao chega depois do que corrige
                for arquivo in sorted(entrada.glob("*.json")):
                    sha = hashlib.sha256(arquivo.read_bytes()).hexdigest()
                    if sha in conhecidos:
                        resultado.arquivos_ja_conhecidos += 1
                        continue
                    lote = ingerir(conn, arquivo)
                    conhecidos.add(sha)
                    resultado.arquivos_ingeridos.append(arquivo.name)
                    if verbose:
                        print(f"  {lote}")

                execucao = regras_run.executar(conn)
                if execucao is not None:
                    resultado.execucao = f"{execucao.execucao_id} ({execucao.escopo})"
                    if verbose:
                        print(f"  {execucao}")

                # sempre, mesmo sem execucao nova: um ciclo anterior pode ter
                # caido entre as regras e a triagem, e o worker retoma
                if triagem.fila(conn):
                    t = triagem.triar(conn, pausa_s=pausa_s, verbose=verbose)
                    resultado.triados, resultado.chamadas_api = t.triados, t.chamadas_api
            finally:
                conn.close()
    except CicloEmAndamento:
        resultado.executado = False
    return resultado


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ciclo da Mesa: ingere, reavalia o delta, tria.")
    p.add_argument("--entrada", type=Path, default=db.RAIZ / "dados" / "entrada",
                   help="pasta com os arquivos de operacoes (formato de dados_nivel_2.json)")
    p.add_argument("--a-cada", type=float, metavar="MIN",
                   help="roda em laco, esperando MIN minutos entre as passadas")
    args = p.parse_args(argv)

    while True:
        inicio = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{inicio}] ciclo: {rodar(args.entrada)}", flush=True)
        if args.a_cada is None:
            return 0
        time.sleep(args.a_cada * 60)


if __name__ == "__main__":
    sys.exit(main())
