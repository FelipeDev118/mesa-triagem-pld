"""Agente de triagem AML: recebe um cliente sinalizado pelas regras determinísticas
e decide, via function calling nativo do LLM, quais ferramentas de nivel_2/tools.py
consultar antes de produzir um parecer estruturado.

O modelo NÃO recebe todas as ferramentas executadas de antemão — ele escolhe, chamada
a chamada, o que precisa ver. Isso é o que separa este agente de um script que sempre
chama tudo (ver enunciado, Nível 2 Parte B).
"""
import json
import os
import time
from typing import Literal, Protocol

from dotenv import load_dotenv
from groq import BadRequestError, Groq, RateLimitError
from pydantic import BaseModel, ValidationError

from cache_parecer import calcular_hash
from dados import aplicar_regras, carregar_e_limpar, montar_flags, ranking_clientes_sinalizados
from observabilidade import Coletor, calcular_custo_usd
from tools import TOOLS_SPEC, historico_cliente

load_dotenv()
CLIENT = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")

TOOLS_OPENAI_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "historico_cliente",
            "description": TOOLS_SPEC["historico_cliente"]["descricao"],
            "parameters": {
                "type": "object",
                "properties": {"cliente_id": {"type": "string"}},
                "required": ["cliente_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "operacoes_do_dia",
            "description": TOOLS_SPEC["operacoes_do_dia"]["descricao"],
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente_id": {"type": "string"},
                    "data": {"type": "string", "description": "formato YYYY-MM-DD"},
                },
                "required": ["cliente_id", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "perfil_canal",
            "description": TOOLS_SPEC["perfil_canal"]["descricao"],
            "parameters": {
                "type": "object",
                "properties": {"cliente_id": {"type": "string"}},
                "required": ["cliente_id"],
            },
        },
    },
]

SYSTEM_PROMPT = """Voce e um agente de triagem de Prevencao a Lavagem de Dinheiro (PLD/AML)
de um banco. Voce recebe um cliente ja sinalizado por regras deterministicas (o calculo de
somas, medias e comparacao com limites ja foi feito fora deste prompt - nunca refaca contas).

Seu trabalho:
1. Decida quais ferramentas voce precisa consultar para entender o caso. Nao chame uma
   ferramenta se ela nao agregar informacao ao caso especifico.
   Regra especifica para operacoes_do_dia (ela exige uma data, e so deve ser chamada com uma
   data que voce realmente tem):
   - Se as flags deterministicas trouxerem "datas_fracionamento" (as datas exatas que
     dispararam a Regra 1 para este cliente), chame operacoes_do_dia para pelo menos uma
     dessas datas - e o motivo de essa ferramenta existir.
   - Se voce NAO tem nenhuma data fornecida nas flags nem observada em outra ferramenta
     (por exemplo, data_min ou data_max de historico_cliente), NAO chame operacoes_do_dia.
     Nunca invente ou chute uma data. Produza o parecer so com as informacoes que ja tem:
     nao investigar um dia especifico por falta de pista concreta e uma decisao valida,
     nao um motivo para adivinhar.
   Voce pode chamar mais de uma ferramenta, em turnos separados, se precisar.
2. Quando tiver informacao suficiente, responda SOMENTE com um JSON (sem texto antes/depois)
   no formato:
{
  "nivel_risco": "baixo" | "medio" | "alto",
  "tipologia_suspeita": "string curta",
  "red_flags": ["lista de strings"],
  "justificativa": "string"
}
Nao chame mais ferramentas depois de decidir responder o JSON final."""


class CacheDeParecer(Protocol):
    """O contrato que rodar_agente() exige de um cache - nada alem disto.

    Era `CacheParecer` concreto. Virou Protocol quando a Mesa de Triagem passou a
    oferecer um segundo backend (mesa/pareceres.py: log append-only em SQLite, em
    vez de mapa sobrescrivivel em JSON). O agente nao precisa saber qual dos dois
    recebeu, e - importante para a direcao da dependencia - nivel_2/ nao pode
    importar mesa/: a camada de sistema conhece a entrega, nunca o contrario.
    """

    def obter(self, hash_entrada: str) -> dict | None: ...

    def salvar(self, hash_entrada: str, resultado: dict) -> object: ...


class ParecerLLM(BaseModel):
    # O enunciado especifica os niveis como baixo/medio/alto (com acento em "medio").
    # Aceitamos as duas grafias porque o modelo alterna entre elas de forma imprevisivel;
    # rejeitar "medio" sem acento seria descartar um parecer valido por detalhe ortografico.
    # A normalizacao para a forma do enunciado acontece na comparacao (confronto.py).
    nivel_risco: Literal["baixo", "medio", "médio", "alto"]
    tipologia_suspeita: str
    red_flags: list[str]
    justificativa: str


def _executar_tool(nome: str, args: dict):
    fn = TOOLS_SPEC[nome]["fn"]
    return fn(**args)


def _chat_com_retry(max_tentativas: int = 5, **kwargs):
    """Free tier do Groq tem limite de tokens/minuto (TPM) baixo. Em vez de deixar o
    lote inteiro quebrar no primeiro 429, esperamos o tempo indicado pela propria API
    (RateLimitError expoe retry_after em segundos) e tentamos de novo.

    Retorna (resposta, tentativas_de_rate_limit) - o contador entra na observabilidade,
    porque espera por rate limit e latencia que o usuario sente mas nao e custo de modelo."""
    for tentativa in range(max_tentativas):
        try:
            return CLIENT.chat.completions.create(**kwargs), tentativa
        except RateLimitError as e:
            espera = getattr(e, "retry_after", None) or (5 * (tentativa + 1))
            try:
                espera = float(str(getattr(e.response, "headers", {}).get("retry-after", espera)))
            except (TypeError, ValueError):
                pass
            espera = max(espera, 2) + 1
            print(f"  rate limit atingido, aguardando {espera:.1f}s (tentativa {tentativa + 1})")
            time.sleep(espera)
    raise RuntimeError("rate limit persistente apos varias tentativas")


def rodar_agente(cliente_id: str, flags: dict, max_turnos: int = 4,
                 coletor: Coletor | None = None, cache: CacheDeParecer | None = None) -> dict:
    """Ponto de entrada publico: consulta o cache antes de chamar o LLM.

    O hash da entrada usa um snapshot de historico_cliente() (nao a base inteira) mais
    as flags, o modelo e a versao do prompt - se nada disso mudou, o parecer e reaproveitado
    em vez de regerado. Ver cache_parecer.py para a justificativa completa: isso existe
    porque medimos, na pratica, o mesmo agente dando nivel_risco diferente para o mesmo
    cliente entre duas execucoes (docs/DECISOES.md).

    Passar `cache=None` (padrao) desliga o cache e sempre chama o LLM - util para medir
    a instabilidade de proposito, como fizemos na auditoria."""
    dados_cliente = historico_cliente(cliente_id)
    hash_entrada = calcular_hash(cliente_id, flags, dados_cliente, MODEL)

    if cache is not None:
        cacheado = cache.obter(hash_entrada)
        if cacheado is not None:
            resultado = dict(cacheado)
            resultado["cache_hit"] = True
            resultado["hash_entrada"] = hash_entrada
            return resultado

    resultado = _rodar_agente_sem_cache(cliente_id, flags, max_turnos, coletor)
    resultado["cache_hit"] = False
    resultado["hash_entrada"] = hash_entrada

    if cache is not None:
        cache.salvar(hash_entrada, resultado)

    return resultado


def _rodar_agente_sem_cache(cliente_id: str, flags: dict, max_turnos: int = 4,
                            coletor: Coletor | None = None) -> dict:
    """Loop de function-calling: o modelo decide quais tools chamar até responder o
    parecer final em JSON. Retorna parecer + métricas (tokens, latência, tools usadas).

    Se `coletor` for passado, cada chamada de API é registrada individualmente nele
    (ver observabilidade.py) — é o que permite separar custo do turno de decisão do
    turno de redação."""
    mensagens = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Cliente {cliente_id} foi sinalizado. Flags deterministicas ja calculadas: "
                f"{json.dumps(flags, ensure_ascii=False)}. Investigue e produza o parecer."
            ),
        },
    ]

    tools_chamadas = []
    tokens_total = 0
    t0 = time.time()

    for turno in range(1, max_turnos + 1):
        t_chamada = time.time()
        try:
            resp, tentativas_rl = _chat_com_retry(
                model=MODEL,
                messages=mensagens,
                tools=TOOLS_OPENAI_SCHEMA,
                tool_choice="auto",
                max_tokens=1500,
                temperature=0.2,
            )
        except BadRequestError as e:
            # Bug conhecido do gpt-oss via Groq: as vezes ele tenta "chamar" uma tool
            # ficticia chamada "JSON" para devolver a resposta final, em vez de so
            # responder em texto. A API rejeita a chamada, mas o conteudo gerado vem
            # dentro do proprio erro (failed_generation) - extraimos de la.
            parecer, erro, tokens_extra = _extrair_parecer_de_erro_tool_json(e)
            tokens_total += tokens_extra
            if parecer is not None or erro:
                return {
                    "cliente_id": cliente_id,
                    "parecer": parecer,
                    "erro_parsing": erro,
                    "texto_bruto": None,
                    "tools_chamadas": tools_chamadas,
                    "tokens_total": tokens_total,
                    "latencia_s": round(time.time() - t0, 2),
                }
            raise
        tokens_total += resp.usage.total_tokens
        msg = resp.choices[0].message

        if coletor is not None:
            # o tipo do turno so e conhecido DEPOIS da resposta: se veio tool_calls, o
            # modelo gastou esta chamada decidindo; se veio texto, gastou redigindo.
            coletor.registrar(
                cliente_id=cliente_id,
                turno=turno,
                tipo_turno="decisao_ferramenta" if msg.tool_calls else "resposta_final",
                modelo=MODEL,
                tokens_entrada=resp.usage.prompt_tokens,
                tokens_saida=resp.usage.completion_tokens,
                tokens_total=resp.usage.total_tokens,
                latencia_s=round(time.time() - t_chamada, 3),
                custo_usd=calcular_custo_usd(
                    MODEL, resp.usage.prompt_tokens, resp.usage.completion_tokens
                ),
                tentativas_rate_limit=tentativas_rl,
            )

        if msg.tool_calls:
            mensagens.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                }
            )
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                resultado = _executar_tool(tc.function.name, args)
                # o PAYLOAD entra junto, nao so a chamada: sem ele, reabrir o caso
                # meses depois mostra o que a base diz HOJE, nao o que o agente viu
                # quando decidiu. Ver mesa/esquema.sql, tabela evidencias.
                tools_chamadas.append(
                    {"tool": tc.function.name, "args": args, "payload": resultado}
                )
                mensagens.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(resultado, ensure_ascii=False, default=str),
                    }
                )
            continue

        texto = msg.content or ""
        latencia = time.time() - t0
        parecer, erro = _parsear_parecer(texto)
        return {
            "cliente_id": cliente_id,
            "parecer": parecer.model_dump() if parecer else None,
            "erro_parsing": erro,
            "texto_bruto": texto if erro else None,
            "tools_chamadas": tools_chamadas,
            "tokens_total": tokens_total,
            "latencia_s": round(latencia, 2),
        }

    return {
        "cliente_id": cliente_id,
        "parecer": None,
        "erro_parsing": "numero maximo de turnos de tool-calling excedido",
        "tools_chamadas": tools_chamadas,
        "tokens_total": tokens_total,
        "latencia_s": round(time.time() - t0, 2),
    }


def _extrair_parecer_de_erro_tool_json(e: BadRequestError):
    """Ver comentario acima: recupera o parecer de dentro de failed_generation quando
    o modelo tenta chamar uma tool ficticia 'JSON' em vez de responder em texto."""
    try:
        body = e.body if isinstance(e.body, dict) else json.loads(str(e.body))
        failed_generation = body["error"]["failed_generation"]
        obj = json.loads(failed_generation)
        args = obj.get("arguments", obj)
        parecer = ParecerLLM(**args)
        return parecer.model_dump(), None, 0
    except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as parse_err:
        return None, f"erro ao extrair parecer de resposta malformada (tool JSON): {parse_err}", 0


def _parsear_parecer(texto: str):
    import re

    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        return None, "nenhum JSON encontrado na resposta"
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return None, f"JSON invalido: {e}"
    try:
        return ParecerLLM(**obj), None
    except ValidationError as e:
        return None, f"schema invalido: {e}"


if __name__ == "__main__":
    df, _ = carregar_e_limpar()
    df = aplicar_regras(df)
    top10 = ranking_clientes_sinalizados(df)
    print(top10[["cliente_id", "total_sinalizacoes", "volume_total_brl"]])

    # cliente com fracionamento, de proposito - e o caso que exercita datas_fracionamento
    row = top10[top10["sinalizacoes_fracionamento"] > 0].iloc[0]
    flags = montar_flags(df, row)
    print(f"\nTestando {row['cliente_id']} com flags: {flags}")

    resultado = rodar_agente(row["cliente_id"], flags)
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
