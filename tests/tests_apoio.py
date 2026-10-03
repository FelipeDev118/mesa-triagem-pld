"""Fakes de resposta da API do Groq, compartilhados entre os testes do agente e
os da Mesa. Extraidos de tests/test_agente.py para nao duplicar a montagem dos
objetos - eram identicos nos dois lugares."""
import json

FLAGS_BASE = {"flag_fracionamento": False, "flag_valor_atipico": True}

PARECER_PADRAO = {
    "nivel_risco": "alto",
    "tipologia_suspeita": "fracionamento",
    "red_flags": ["varias operacoes no mesmo dia"],
    "justificativa": "Operacoes fracionadas no mesmo dia.",
}


class _Function:
    def __init__(self, nome, args):
        self.name = nome
        self.arguments = json.dumps(args)


class _ToolCall:
    def __init__(self, nome, args):
        self.id = "call_1"
        self.type = "function"
        self.function = _Function(nome, args)

    def model_dump(self):
        return {"id": self.id, "type": self.type,
                "function": {"name": self.function.name,
                             "arguments": self.function.arguments}}


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Usage:
    def __init__(self):
        self.prompt_tokens = 100
        self.completion_tokens = 50
        self.total_tokens = 150


class _Choice:
    def __init__(self, message):
        self.message = message


class _Response:
    def __init__(self, message):
        self.choices = [_Choice(message)]
        self.usage = _Usage()


def resposta_com_tool_call(nome="historico_cliente", args=None):
    return _Response(_Message(tool_calls=[_ToolCall(nome, args or {"cliente_id": "CLI-014"})]))


def resposta_final(parecer=None):
    return _Response(_Message(content=json.dumps(parecer or PARECER_PADRAO, ensure_ascii=False)))
