"""Passo 1.3 - traz o cache JSON existente para o log de pareceres.

Por que importar em vez de comecar do zero: as 50 entradas de
outputs/cache_pareceres.json custaram chamadas de API reais e sao a evidencia de
tudo que a entrega afirma. Descarta-las para "comecar limpo" jogaria fora o
historico exatamente no passo em que o sistema passa a ter historico.

O que este script NAO faz: inventar um timestamp. O cache nao guarda quando cada
parecer foi gerado, entao criado_em recebe o mtime do arquivo e a linha e marcada
com origem_registro='importado_cache'. Um timestamp inferido que se passe por
observado e pior que a ausencia dele.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import mesa  # noqa: F401
from cache_parecer import CACHE_PATH

from mesa.db import conectar
from mesa.pareceres import RepositorioPareceres


def importar(conn: sqlite3.Connection, caminho: Path = CACHE_PATH) -> dict:
    if not Path(caminho).exists():
        return {"arquivo": str(caminho), "encontrados": 0, "importados": 0, "ja_existentes": 0}

    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    mtime = datetime.fromtimestamp(Path(caminho).stat().st_mtime, tz=timezone.utc)
    criado_em = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")

    repo = RepositorioPareceres(conn)
    ja_existentes = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT hash_entrada FROM pareceres "
            "WHERE origem_registro = 'importado_cache'"
        )
    }

    importados = 0
    for hash_entrada, resultado in dados.items():
        if hash_entrada in ja_existentes:
            continue  # idempotente: reimportar nao duplica o historico
        repo.salvar(
            hash_entrada, resultado,
            origem_registro="importado_cache", criado_em=criado_em,
            # sem vinculo a alerta: estes pareceres sao anteriores aos alertas de
            # hoje. Quem os vincular sera o worker, via reaproveitamento.
            vincular_alerta=False,
        )
        importados += 1

    return {
        "arquivo": str(caminho),
        "encontrados": len(dados),
        "importados": importados,
        "ja_existentes": len(dados) - importados,
        "criado_em_inferido": criado_em,
    }


if __name__ == "__main__":
    caminho = Path(sys.argv[1]) if len(sys.argv) > 1 else CACHE_PATH
    with conectar() as conn:
        r = importar(conn, caminho)
    print(f"{r['encontrados']} entradas em {r['arquivo']}: "
          f"{r['importados']} importadas, {r['ja_existentes']} ja no store "
          f"(criado_em inferido: {r.get('criado_em_inferido', '-')})")
