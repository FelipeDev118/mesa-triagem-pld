"""Teste de integração do agente (nivel_2/agente.py) com o cliente Groq mockado —
roda sem rede e sem rate limit (ver docs/DECISOES.md, "Testes automatizados das regras").

As classes Fake* abaixo reproduzem só a forma dos objetos que o código de agente.py
efetivamente lê (resp.choices[0].message, msg.tool_calls, tc.function, resp.usage) —
não a SDK inteira da Groq.
"""
import json

import agente
from cache_parecer import CacheParecer

CLIENTE_REAL = "CLI-014"  # existe em dados/dados_nivel_2.json; historico_cliente() roda contra dado real
FLAGS_BASE = {"flag_fracionamento": False, "flag_valor_atipico": True}

PARECER_VALIDO = {
    "nivel_risco": "alto",
    "tipologia_suspeita": "valor atipico",
    "red_flags": ["operacao muito acima da mediana do cliente"],
    "justificativa": "operacao destoa do padrao historico do cliente",
}


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = FakeFunction(name, arguments)

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=10):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message, prompt_tokens=10, completion_tokens=10):
        self.choices = [FakeChoice(message)]
        self.usage = FakeUsage(prompt_tokens, completion_tokens)


def _resposta_com_tool_call(tool="historico_cliente", args=None):
    args = args if args is not None else {"cliente_id": CLIENTE_REAL}
    return FakeResponse(
        FakeMessage(tool_calls=[FakeToolCall("call-1", tool, json.dumps(args))])
    )


def _resposta_final(parecer=None):
    return FakeResponse(FakeMessage(content=json.dumps(parecer or PARECER_VALIDO)))


def test_agente_chama_tool_e_produz_parecer_valido(monkeypatch):
    respostas = iter([_resposta_com_tool_call(), _resposta_final()])
    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", lambda **kw: next(respostas))

    resultado = agente.rodar_agente(CLIENTE_REAL, FLAGS_BASE)

    assert resultado["erro_parsing"] is None
    assert resultado["parecer"]["nivel_risco"] == "alto"
    # o payload da ferramenta entra na chamada registrada (passo 1.4 do ROADMAP):
    # e o que permite reabrir o caso vendo o que o agente viu, nao o que a base
    # diz hoje. Por isso a asercao confere tool/args e a presenca do payload, em
    # vez de igualdade exata com um dict de 2 chaves.
    (chamada,) = resultado["tools_chamadas"]
    assert chamada["tool"] == "historico_cliente"
    assert chamada["args"] == {"cliente_id": CLIENTE_REAL}
    assert chamada["payload"]["cliente_id"] == CLIENTE_REAL
    assert resultado["cache_hit"] is False


def test_agente_usa_cache_e_nao_chama_llm_de_novo(monkeypatch, tmp_path):
    chamadas = {"n": 0}

    def fake_create(**kwargs):
        chamadas["n"] += 1
        return _resposta_final()

    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", fake_create)

    cache = CacheParecer(caminho=tmp_path / "cache.json")

    primeiro = agente.rodar_agente(CLIENTE_REAL, FLAGS_BASE, cache=cache)
    segundo = agente.rodar_agente(CLIENTE_REAL, FLAGS_BASE, cache=cache)

    assert primeiro["cache_hit"] is False
    assert segundo["cache_hit"] is True
    assert segundo["parecer"] == primeiro["parecer"]
    assert chamadas["n"] == 1


def test_agente_esgota_max_turnos_sem_resposta_final(monkeypatch):
    # o fake nunca devolve texto final, so tool_calls: exercita o caminho de erro
    # quando o modelo nao converge dentro do limite de turnos.
    monkeypatch.setattr(
        agente.CLIENT.chat.completions, "create", lambda **kw: _resposta_com_tool_call()
    )

    resultado = agente.rodar_agente(CLIENTE_REAL, FLAGS_BASE, max_turnos=2)

    assert resultado["parecer"] is None
    assert "numero maximo de turnos" in resultado["erro_parsing"]


def test_agente_rejeita_parecer_fora_do_schema(monkeypatch):
    respostas = iter(
        [_resposta_com_tool_call(), _resposta_final(parecer={"nivel_risco": "critico", "tipologia_suspeita": "x"})]
    )
    monkeypatch.setattr(agente.CLIENT.chat.completions, "create", lambda **kw: next(respostas))

    resultado = agente.rodar_agente(CLIENTE_REAL, FLAGS_BASE)

    assert resultado["parecer"] is None
    assert resultado["erro_parsing"] is not None
