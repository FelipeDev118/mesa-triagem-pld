"""Nivel 3 - Trilha B: o agente do Nivel 2, agora consumindo as ferramentas por MCP.

Diferenca em relacao a nivel_2/agente.py: aquele importa tools.py e chama a funcao Python
direto (`TOOLS_SPEC[nome]["fn"](**args)`). Este sobe nivel_3/mcp_server.py como subprocesso
stdio e chama `session.call_tool(...)` - o agente nao tem mais acesso ao codigo das
ferramentas, so ao contrato que o servidor publica.

Duas consequencias praticas:
1. A lista de ferramentas passa a ser DESCOBERTA em runtime (`session.list_tools()`), em vez
   de estar hardcoded no agente. Se o servidor publicar uma quarta ferramenta amanha, o agente
   a enxerga sem alteracao de codigo.
2. O schema enviado ao LLM e traduzido do que o servidor declarou, nao escrito a mao.

Rodar:
    python nivel_3/agente_mcp.py            # 1 cliente, para inspecao
    python nivel_3/agente_mcp.py --lote     # os 10 clientes, salva em outputs/
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "nivel_2"))

from dotenv import load_dotenv
from groq import BadRequestError, Groq, RateLimitError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agente import ParecerLLM, SYSTEM_PROMPT, _parsear_parecer
from dados import aplicar_regras, carregar_e_limpar, montar_flags, ranking_clientes_sinalizados

load_dotenv(RAIZ / ".env")
CLIENT = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")

PARAMETROS_SERVIDOR = StdioServerParameters(
    command=sys.executable,
    args=[str(Path(__file__).resolve().parent / "mcp_server.py")],
)


def _mcp_para_openai(ferramentas) -> list[dict]:
    """Traduz o que o servidor MCP publicou para o formato de tools que o LLM espera.
    Nada aqui e hardcoded: os nomes, descricoes e schemas vem do servidor."""
    return [
        {
            "type": "function",
            "function": {
                "name": f.name,
                "description": f.description,
                "parameters": f.input_schema,
            },
        }
        for f in ferramentas
    ]


def _chat_com_retry(max_tentativas: int = 5, **kwargs):
    for tentativa in range(max_tentativas):
        try:
            return CLIENT.chat.completions.create(**kwargs)
        except RateLimitError:
            espera = 5 * (tentativa + 1)
            print(f"  rate limit, aguardando {espera}s", file=sys.stderr)
            time.sleep(espera)
    raise RuntimeError("rate limit persistente")


async def rodar_agente_mcp(session: ClientSession, cliente_id: str, flags: dict,
                           max_turnos: int = 4) -> dict:
    ferramentas_mcp = (await session.list_tools()).tools
    tools_schema = _mcp_para_openai(ferramentas_mcp)

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

    for _ in range(max_turnos):
        try:
            resp = _chat_com_retry(
                model=MODEL,
                messages=mensagens,
                tools=tools_schema,
                tool_choice="auto",
                max_tokens=1500,
                temperature=0.2,
            )
        except BadRequestError as e:
            from agente import _extrair_parecer_de_erro_tool_json

            parecer, erro, _ = _extrair_parecer_de_erro_tool_json(e)
            return {
                "cliente_id": cliente_id,
                "transporte": "mcp",
                "parecer": parecer,
                "erro_parsing": erro,
                "tools_chamadas": tools_chamadas,
                "tokens_total": tokens_total,
                "latencia_s": round(time.time() - t0, 2),
            }

        tokens_total += resp.usage.total_tokens
        msg = resp.choices[0].message

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
                # AQUI esta a diferenca: chamada via protocolo MCP, nao import direto
                resultado_mcp = await session.call_tool(tc.function.name, args)
                conteudo = resultado_mcp.content[0].text if resultado_mcp.content else "{}"
                # idem nivel_2/agente.py: o payload faz parte da evidencia
                try:
                    payload = json.loads(conteudo)
                except json.JSONDecodeError:
                    payload = conteudo
                tools_chamadas.append(
                    {"tool": tc.function.name, "args": args, "payload": payload}
                )
                mensagens.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": conteudo}
                )
            continue

        parecer, erro = _parsear_parecer(msg.content or "")
        return {
            "cliente_id": cliente_id,
            "transporte": "mcp",
            "parecer": parecer.model_dump() if parecer else None,
            "erro_parsing": erro,
            "tools_chamadas": tools_chamadas,
            "tokens_total": tokens_total,
            "latencia_s": round(time.time() - t0, 2),
        }

    return {
        "cliente_id": cliente_id,
        "transporte": "mcp",
        "parecer": None,
        "erro_parsing": "numero maximo de turnos excedido",
        "tools_chamadas": tools_chamadas,
        "tokens_total": tokens_total,
        "latencia_s": round(time.time() - t0, 2),
    }


async def main(lote: bool):
    df, _ = carregar_e_limpar()
    df = aplicar_regras(df)
    top10 = ranking_clientes_sinalizados(df, top_n=10)
    alvos = top10 if lote else top10.head(1)

    async with stdio_client(PARAMETROS_SERVIDOR) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            publicadas = (await session.list_tools()).tools
            print(f"Ferramentas descobertas via MCP: {[f.name for f in publicadas]}")

            resultados = []
            for _, row in alvos.iterrows():
                flags = montar_flags(df, row)
                print(f"Processando {row['cliente_id']} via MCP...")
                r = await rodar_agente_mcp(session, row["cliente_id"], flags)
                resultados.append(r)
                if lote:
                    await asyncio.sleep(8)

    if lote:
        destino = RAIZ / "outputs" / "pareceres_lote_mcp.json"
        with open(destino, "w", encoding="utf-8") as f:
            json.dump(resultados, f, indent=2, ensure_ascii=False)
        print(f"\nSalvo em {destino}")
    else:
        print(json.dumps(resultados[0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(lote="--lote" in sys.argv))
