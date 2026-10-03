"""Passos 1.2 a 1.6 - o parecer deixa de ser cache e vira registro.

`RepositorioPareceres` expoe exatamente a interface de `CacheParecer`
(`obter`/`salvar`/`__len__`), de proposito: e o que permite trocar um pelo outro
sem reescrever o agente. Mas o que acontece por baixo e outra coisa.

  CacheParecer                      RepositorioPareceres
  ------------                      --------------------
  mapa hash -> parecer              log append-only
  salvar() sobrescreve              salvar() insere uma linha nova
  perde o historico                 o historico E o dado
  so o parecer                      + evidencia, aderencia, custo por chamada

A pergunta que o cache nao responde e que este modulo responde: "por que este
cliente foi classificado como alto risco em 15/03?" - mesmo que o parecer tenha
mudado depois, mesmo que a base tenha mudado depois.
"""
import json
import os
import sqlite3
from typing import Any

from cache_parecer import VERSAO_PROMPT

from mesa.db import agora_utc

# Espelha agente.MODEL sem importar agente: aquele modulo instancia o cliente
# Groq na importacao e exige GROQ_API_KEY. Ler o store nao pode depender de ter
# chave de API - a API e um detalhe de quem ESCREVE o parecer, nao de quem le.
MODELO_PADRAO = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")

CAMPOS_PARECER = ("nivel_risco", "tipologia_suspeita", "red_flags", "justificativa")


class RepositorioPareceres:
    """Drop-in para CacheParecer, com trilha de auditoria por baixo."""

    def __init__(self, conn: sqlite3.Connection, modelo: str = MODELO_PADRAO,
                 versao_prompt: str = VERSAO_PROMPT):
        self.conn = conn
        self.modelo = modelo
        self.versao_prompt = versao_prompt

    # ---------- interface de CacheParecer ----------

    def obter(self, hash_entrada: str) -> dict | None:
        """O parecer ATUAL para esta entrada: a linha mais recente do hash.

        Leitura pura - nao grava nada. O registro de reaproveitamento e um ato
        separado e explicito (`reaproveitar`), porque quem sabe para qual alerta
        o parecer esta sendo reusado e o chamador, nao o repositorio.
        """
        linha = self._linha_atual(hash_entrada)
        return self._para_resultado(linha) if linha else None

    def salvar(self, hash_entrada: str, resultado: dict, alerta_id: int | None = None,
               origem_registro: str = "agente", criado_em: str | None = None,
               vincular_alerta: bool = True) -> int:
        """Insere um parecer novo. Nunca atualiza - o trigger do banco garante.

        `alerta_id` omitido e resolvido para o alerta mais recente do cliente, se
        houver: e quase sempre o que se quer, e evita que o chamador tenha que
        carregar esse id so para repassa-lo. Sem alerta nenhum (agente rodado
        fora da fila) a coluna fica NULL, que e um estado valido.

        `vincular_alerta=False` desliga essa resolucao e forca NULL. E o caso da
        importacao do cache antigo (mesa/importar_cache.py): aqueles pareceres
        foram gerados ANTES de o alerta existir, e amarra-los ao alerta de hoje
        seria antedatar o vinculo. Custou um teste vermelho para aparecer - com
        o vinculo automatico, o worker via "este alerta ja tem parecer" e pulava
        o reaproveitamento, que e justamente o mecanismo que o passo 1.5 cria.
        """
        parecer = resultado.get("parecer") or {}
        cliente_id = resultado["cliente_id"]
        if alerta_id is None and vincular_alerta:
            alerta_id = self.alerta_atual(cliente_id)

        cur = self.conn.execute(
            "INSERT INTO pareceres (alerta_id, cliente_id, hash_entrada, nivel_risco, "
            "tipologia_suspeita, red_flags_json, justificativa, erro_parsing, "
            "texto_bruto, modelo, versao_prompt, transporte, origem_registro, "
            "tokens_total, latencia_s, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alerta_id, cliente_id, hash_entrada,
                parecer.get("nivel_risco"),
                parecer.get("tipologia_suspeita"),
                json.dumps(parecer.get("red_flags"), ensure_ascii=False)
                if parecer.get("red_flags") is not None else None,
                parecer.get("justificativa"),
                resultado.get("erro_parsing"),
                resultado.get("texto_bruto"),
                self.modelo, self.versao_prompt,
                resultado.get("transporte", "import_direto"),
                origem_registro,
                resultado.get("tokens_total"),
                resultado.get("latencia_s"),
                criado_em or agora_utc(),
            ),
        )
        parecer_id = cur.lastrowid
        self._gravar_evidencias(parecer_id, resultado.get("tools_chamadas") or [])
        self.conn.commit()
        return parecer_id

    def __len__(self) -> int:
        """Entradas distintas, nao linhas: e o numero que `CacheParecer` reportava
        (quantos casos estao cobertos), e continua sendo o util para o operador."""
        return self.conn.execute(
            "SELECT COUNT(DISTINCT hash_entrada) FROM pareceres"
        ).fetchone()[0]

    # ---------- alem do cache ----------

    def reaproveitar(self, hash_entrada: str, alerta_id: int) -> int | None:
        """Passo 1.5 - vincula um parecer ja existente a um alerta novo.

        O hash nao inclui alerta_id (de proposito: e o que faz o reaproveitamento
        funcionar entre execucoes). Isso cria um caso: uma execucao nova de regras
        gera um alerta novo para um cliente cujos dados nao mudaram; o hash bate e
        nenhuma linha e escrita - o alerta fica sem parecer e a trilha tem um
        buraco.

        A saida nao e regerar (custaria uma chamada de LLM para o mesmo resultado)
        nem apontar dois alertas para a mesma linha (perderia a data em que este
        alerta foi triado). E inserir uma linha nova, com o mesmo conteudo e
        `reaproveitado_de` apontando para a origem: cada alerta tem seu parecer, e
        continua visivel que o texto foi gerado antes.
        """
        origem = self._linha_atual(hash_entrada)
        if origem is None:
            return None

        cur = self.conn.execute(
            "INSERT INTO pareceres (alerta_id, cliente_id, hash_entrada, "
            "reaproveitado_de, nivel_risco, tipologia_suspeita, red_flags_json, "
            "justificativa, erro_parsing, texto_bruto, modelo, versao_prompt, "
            "transporte, origem_registro, tokens_total, latencia_s, criado_em) "
            "SELECT ?, cliente_id, hash_entrada, COALESCE(reaproveitado_de, id), "
            "nivel_risco, tipologia_suspeita, red_flags_json, justificativa, "
            "erro_parsing, texto_bruto, modelo, versao_prompt, transporte, "
            "origem_registro, tokens_total, latencia_s, ? FROM pareceres WHERE id = ?",
            (alerta_id, agora_utc(), origem["id"]),
        )
        novo_id = cur.lastrowid

        # a evidencia acompanha a copia: o caso tem que ser reabrivel pelo alerta
        self.conn.execute(
            "INSERT INTO evidencias (parecer_id, ordem, tool, args_json, payload_json) "
            "SELECT ?, ordem, tool, args_json, payload_json FROM evidencias "
            "WHERE parecer_id = ?",
            (novo_id, origem["id"]),
        )
        self.conn.commit()
        return novo_id

    def gravar_aderencia(self, parecer_id: int, aderencia: dict) -> None:
        """Passo 1.6 - o grounding check gravado junto do parecer que auditou."""
        self.conn.execute(
            "INSERT OR REPLACE INTO aderencia (parecer_id, fundamentado, motivo, "
            "valores_confirmados_json, valores_nao_encontrados_json, "
            "atipicos_incorretos_json, marcas_json, verificado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parecer_id,
                int(bool(aderencia.get("fundamentado"))),
                aderencia.get("motivo", ""),
                json.dumps(aderencia.get("valores_confirmados", []), ensure_ascii=False, default=str),
                json.dumps(aderencia.get("valores_nao_encontrados", []), ensure_ascii=False, default=str),
                json.dumps(aderencia.get("atipicos_incorretos", []), ensure_ascii=False, default=str),
                json.dumps(aderencia.get("marcas", []), ensure_ascii=False, default=str),
                agora_utc(),
            ),
        )
        self.conn.commit()

    def gravar_chamadas(self, parecer_id: int, chamadas: list[Any]) -> int:
        """Passo 1.6 - as linhas do Coletor de observabilidade.py, persistidas.

        Aceita as dataclasses ChamadaLLM ou dicts equivalentes."""
        registros = [c if isinstance(c, dict) else vars(c) for c in chamadas]
        if not registros:
            return 0
        self.conn.executemany(
            "INSERT INTO chamadas_llm (parecer_id, cliente_id, turno, tipo_turno, "
            "modelo, tokens_entrada, tokens_saida, tokens_total, latencia_s, "
            "custo_usd, transporte, tentativas_rate_limit, registrado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    parecer_id, r["cliente_id"], r["turno"], r["tipo_turno"], r["modelo"],
                    r["tokens_entrada"], r["tokens_saida"], r["tokens_total"],
                    r["latencia_s"], r["custo_usd"],
                    r.get("transporte", "import_direto"),
                    r.get("tentativas_rate_limit", 0), agora_utc(),
                )
                for r in registros
            ],
        )
        self.conn.commit()
        return len(registros)

    def alerta_atual(self, cliente_id: str) -> int | None:
        linha = self.conn.execute(
            "SELECT id FROM alertas WHERE cliente_id = ? ORDER BY execucao_id DESC, id DESC "
            "LIMIT 1", (cliente_id,)
        ).fetchone()
        return int(linha[0]) if linha else None

    def do_alerta(self, alerta_id: int) -> dict | None:
        """O parecer atual de um alerta - o que a tela de caso vai consumir."""
        linha = self.conn.execute(
            "SELECT * FROM pareceres WHERE alerta_id = ? ORDER BY criado_em DESC, id DESC "
            "LIMIT 1", (alerta_id,)
        ).fetchone()
        return self._para_resultado(linha) if linha else None

    # ---------- internos ----------

    def _linha_atual(self, hash_entrada: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pareceres WHERE hash_entrada = ? "
            "ORDER BY criado_em DESC, id DESC LIMIT 1",
            (hash_entrada,),
        ).fetchone()

    def _gravar_evidencias(self, parecer_id: int, tools_chamadas: list[dict]) -> None:
        if not tools_chamadas:
            return
        self.conn.executemany(
            "INSERT INTO evidencias (parecer_id, ordem, tool, args_json, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    parecer_id, ordem, tc["tool"],
                    json.dumps(tc.get("args", {}), ensure_ascii=False, default=str),
                    # payload so existe a partir do passo 1.4; um parecer gravado
                    # antes disso tem a chamada registrada sem o retorno
                    json.dumps(tc.get("payload"), ensure_ascii=False, default=str),
                )
                for ordem, tc in enumerate(tools_chamadas)
            ],
        )

    def _evidencias(self, parecer_id: int) -> list[dict]:
        linhas = self.conn.execute(
            "SELECT tool, args_json, payload_json FROM evidencias "
            "WHERE parecer_id = ? ORDER BY ordem", (parecer_id,)
        ).fetchall()
        chamadas = []
        for linha in linhas:
            chamada = {"tool": linha["tool"], "args": json.loads(linha["args_json"])}
            payload = json.loads(linha["payload_json"])
            if payload is not None:
                chamada["payload"] = payload
            chamadas.append(chamada)
        return chamadas

    def _para_resultado(self, linha: sqlite3.Row) -> dict:
        """Reconstroi o dict que o agente espera - mesma forma que CacheParecer
        devolvia, para que a troca seja invisivel para quem consome."""
        tem_parecer = linha["nivel_risco"] is not None
        parecer = {
            "nivel_risco": linha["nivel_risco"],
            "tipologia_suspeita": linha["tipologia_suspeita"],
            "red_flags": json.loads(linha["red_flags_json"]) if linha["red_flags_json"] else [],
            "justificativa": linha["justificativa"],
        } if tem_parecer else None

        return {
            "cliente_id": linha["cliente_id"],
            "parecer": parecer,
            "erro_parsing": linha["erro_parsing"],
            "texto_bruto": linha["texto_bruto"],
            "tools_chamadas": self._evidencias(linha["id"]),
            "tokens_total": linha["tokens_total"],
            "latencia_s": linha["latencia_s"],
            "cache_hit": False,
            "hash_entrada": linha["hash_entrada"],
            "parecer_id": linha["id"],
        }
