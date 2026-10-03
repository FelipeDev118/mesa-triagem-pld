"""Verificacao de aderencia: confere se os valores em R$ que a justificativa do LLM cita
existem de fato nos dados do cliente, antes de aceitar o parecer como fundamentado.

Motivacao concreta, nao hipotetica: numa execucao anterior, o parecer de CLI-005 citou
"a operacao de 2024-05-07 (R$ 409,16)" como a operacao atipica - so que R$ 409,16 esta
ABAIXO da mediana do cliente (nao e a operacao sinalizada pela Regra 2) e o ano estava
errado (2024, nao 2026). O texto era bem escrito e soava tecnico. Um analista humano
lendo so a justificativa nao teria como perceber o erro sem reabrir a base.

Esta verificacao automatiza exatamente essa conferencia: extrai todo valor em R$ citado
na justificativa e confere contra os dados REAIS do cliente - operacoes individuais,
somas por data (relevante para fracionamento) e agregados do cliente (soma, media,
mediana). Deliberadamente SEM LLM: usar um modelo para auditar outro reintroduziria o
mesmo problema que estamos tentando pegar.

O que isto NAO faz: nao entende o CONTEXTO da citacao alem de um filtro simples de
limiares textuais ("superior a R$X" nao e uma transacao, e um qualificador). E uma
verificacao de EXISTENCIA, nao de RACIOCINIO - ainda assim, e suficiente para pegar o
caso CLI-005: la, o numero citado nem existia como operacao nem como agregado do
cliente. Ver DECISOES.md para a distincao entre isso e um "juiz" completo.

Duas categorias de falso positivo encontradas ao validar contra dados reais, e como
foram tratadas:
  1. Soma de operacoes de UMA DATA especifica (ex.: "4 operacoes em 26/05, totalizando
     R$71.297,68") - nao e a soma total do cliente, e a soma do dia. Adicionado como
     referencia extra quando o cliente tem flag_fracionamento.
  2. Limiares textuais ("valores superiores a R$3.000") - nao sao uma transacao citada,
     sao uma qualificacao vaga. Filtrados por palavras-gatilho antes do valor.
"""
import re
from dataclasses import dataclass, field

import pandas as pd

# R$ 14.326,29 | R$14.326,29 | R$ 7.330 | R$312,54 | R$ 88.750,8 | R$71,297.68 (formato
# americano, milhar por virgula) | R$14.3k (abreviado, "k" = mil - ambos vistos na base real)
# Alternativas com separador de milhar (BR: ponto+3 digitos; US: virgula+3 digitos) vem
# primeiro, senao a captura para no meio do numero e trunca (era o bug: "71,297.68"
# virava "71,29" -> 71.29).
PADRAO_VALOR_RS = re.compile(
    r"R\$\s?("
    r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?"    # BR com milhar: 14.326,29 | 7.330
    r"|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"   # US com milhar: 71,297.68 | 71,297
    r"|\d+,\d{1,2}"                          # BR sem milhar: 312,54
    r"|\d+\.\d{1,2}(?!\d)"                  # US sem milhar: 312.54
    r"|\d+"                                    # inteiro puro: 88
    r")\s*(?P<k>[kK])?\b"
)

# O modelo tambem escreve valor sem "R$", com sufixo "BRL" - e formato numerico
# inconsistente entre chamadas: "21.261,01 BRL" (formato BR) ou "5016.62 BRL" (ponto
# como decimal, sem separador de milhar). Captura ampla; o parsing decide o formato.
PADRAO_VALOR_BRL_SUFIXO = re.compile(
    r"(\d[\d.,]*\d|\d+)\s*BRL\b", re.IGNORECASE
)

# Palavras que, aparecendo logo antes do valor, indicam limiar/qualificador, nao uma
# transacao ou agregado especifico sendo citado ("valores superiores a R$3.000").
# "entre" entra aqui pelo mesmo motivo: "entre R$14.3k e R$19.4k" e faixa aproximada
# (o modelo arredondando os extremos reais, 14326.29 e 19418.96), nao uma citacao
# exata - achado real em CLI-029/CLI-017 ao reauditar o lote reexecutado.
QUALIFICADORES = re.compile(
    r"(superior(?:es)?\s+a|acima\s+de|abaixo\s+de|menos\s+de|mais\s+de|"
    r"pr[oó]xim[oa]s?\s+(?:a|de)|cerca\s+de|aproximadamente|em\s+torno\s+de|"
    r"at[ée]|entre)\s*$",
    re.IGNORECASE,
)

# O segundo limite de uma faixa ("...e R$19.4k") nao tem uma palavra-gatilho antes de
# si - "e" sozinho e comum demais para servir de marcador sem falsos positivos. Em vez
# disso, propaga o "e_limiar" do primeiro limite para o segundo quando os dois estao
# ligados so por espaco + "e" + espaco (+ o "R$" do proximo valor, ja que o inicio
# capturado e apos o "R$" - ver extrair_valores).
CONECTOR_FAIXA = re.compile(r"^\s*e\s*(?:R\$)?\s*$", re.IGNORECASE)

TOLERANCIA_R = 0.5  # cobre "R$7.330" citando 7330.00 sem casas decimais
# "R$58.6k" (achado real em CLI-007) e o agregado 58601.43 arredondado para 1 casa
# decimal de milhar - erro de arredondamento de ate 50 (metade da menor unidade
# representada). Tolerancia normal (0.5) rejeitaria uma citacao correta so por causa
# da abreviacao; nao e o mesmo tipo de erro que citar um numero que nao existe.
TOLERANCIA_ABREVIADO_K = 50.0
JANELA_CONTEXTO = 25  # caracteres antes do valor, para checar qualificador

# Verificacao de CONTEXTO (nao so de existencia): quando o texto liga um valor a palavra
# "atipic*", esse valor tem que ser de uma operacao com flag_valor_atipico=True. E o erro
# do CLI-005: R$409,16 EXISTE na base (entao passa na checagem de existencia), mas foi
# citado como a operacao atipica quando esta abaixo da mediana e nao tem a flag.
JANELA_ATIPICO = 160  # caracteres ao redor do valor onde procuramos a palavra
PADRAO_ATIPICO = re.compile(r"at[ií]pic", re.IGNORECASE)


def _parsear_valor_brl(bruto: str) -> float:
    """Lida com os formatos que o modelo produz, sem assumir um so:
      '14.326,29' -> 14326.29   (BR: ponto de milhar, virgula decimal)
      '71,297.68' -> 71297.68   (US: virgula de milhar, ponto decimal)
      '7.330'     -> 7330.0     (BR: ponto de milhar, sem decimais)
      '5016.62'   -> 5016.62    (US: ponto decimal, sem milhar)
    Quando os dois separadores aparecem, o DECIMAL e o que vem por ultimo na string
    (BR: ponto...virgula: 14.326,29; US: virgula...ponto: 71,297.68) - o outro e milhar
    e e descartado. Quando so um aparece, um ponto seguido de exatamente 2 digitos no
    fim e decimal americano; senao e milhar BR."""
    tem_ponto, tem_virgula = "." in bruto, "," in bruto
    if tem_ponto and tem_virgula:
        if bruto.rfind(",") > bruto.rfind("."):
            return float(bruto.replace(".", "").replace(",", "."))  # BR
        return float(bruto.replace(",", ""))  # US
    if tem_virgula:
        return float(bruto.replace(".", "").replace(",", "."))
    if re.search(r"\.\d{1,2}$", bruto):
        return float(bruto)
    return float(bruto.replace(".", ""))


def extrair_valores(texto: str) -> list[dict]:
    """Retorna [{'valor': float, 'e_limiar': bool}, ...] - separa valores citados como
    transacao/agregado de valores citados como limiar textual.

    Deduplica por posicao para nao contar duas vezes um valor que casasse nos dois
    padroes (ex.: "R$ 100,00 BRL"). Um "k" logo apos o numero ("R$14.3k") e abreviacao
    de mil - visto na base real em faixas como "entre R$14.3k e R$19.4k"."""
    return [
        {k: v for k, v in item.items() if k not in _CAMPOS_DE_POSICAO}
        for item in _extrair_com_posicao(texto)
    ]


_CAMPOS_DE_POSICAO = ("inicio", "fim", "trecho_inicio", "trecho_fim")


def _extrair_com_posicao(texto: str) -> list[dict]:
    """O mesmo que extrair_valores(), mais ONDE cada valor esta no texto.

    Existe para a tela da Mesa de Triagem (Fase 3) marcar cada numero citado e
    liga-lo a sua fonte. A alternativa - a tela procurar os valores no texto por
    conta propria - seria uma segunda copia deste parser em JavaScript, e este
    parser ja teve cinco bugs de formato (americano, "k", "BRL"...). Um parser so,
    aqui, e as posicoes gravadas junto da classificacao que elas ilustram.

    `trecho_inicio`/`trecho_fim` cobrem o que o leitor ve ("R$ 6.913,84"), com o
    prefixo e sem o espaco final que o `\\s*` do padrao engole."""
    achados: dict[int, dict] = {}
    for padrao in (PADRAO_VALOR_RS, PADRAO_VALOR_BRL_SUFIXO):
        for m in padrao.finditer(texto):
            try:
                valor = _parsear_valor_brl(m.group(1))
            except ValueError:
                continue  # captura ampla do padrao BRL pode pegar lixo tipo "1.2.3"
            tolerancia = TOLERANCIA_R
            if m.groupdict().get("k"):
                valor *= 1000
                tolerancia = TOLERANCIA_ABREVIADO_K
            contexto_antes = texto[max(0, m.start() - JANELA_CONTEXTO):m.start()]
            janela = texto[max(0, m.start() - JANELA_ATIPICO):m.end() + JANELA_ATIPICO]
            achados[m.start(1)] = {
                "valor": valor,
                "e_limiar": bool(QUALIFICADORES.search(contexto_antes)),
                "citado_como_atipico": bool(PADRAO_ATIPICO.search(janela)),
                "tolerancia": tolerancia,
                "inicio": m.start(1),
                "fim": m.end(),
                "trecho_inicio": m.start(),
                "trecho_fim": len(texto[:m.end()].rstrip()),
            }

    ordenados = [achados[k] for k in sorted(achados)]
    # propaga e_limiar do 1o limite de uma faixa ("entre X e Y") para o 2o: os dois
    # ficam ligados so por espaco + "e" + espaco, sem palavra-gatilho propria antes
    # do segundo valor.
    for anterior, atual in zip(ordenados, ordenados[1:]):
        if anterior["e_limiar"] and not atual["e_limiar"]:
            entre = texto[anterior["fim"]:atual["inicio"]]
            if CONECTOR_FAIXA.match(entre):
                atual["e_limiar"] = True

    return ordenados


@dataclass
class Aderencia:
    cliente_id: str
    valores_citados: list[float] = field(default_factory=list)
    valores_limiar_ignorados: list[float] = field(default_factory=list)
    valores_confirmados: list[dict] = field(default_factory=list)   # {"valor":, "fonte":}
    valores_nao_encontrados: list[float] = field(default_factory=list)
    # valores que EXISTEM na base mas foram citados como "atipicos" sem ter a flag
    atipicos_incorretos: list[dict] = field(default_factory=list)
    # Um item por valor encontrado no texto, na ordem em que aparece:
    # {inicio, fim, valor, classe, fonte}. classe e 'confirmado',
    # 'nao_encontrado', 'atipico_incorreto' ou 'limiar'. E o que a tela usa para
    # marcar o numero - posicao e classificacao saem da MESMA execucao.
    marcas: list[dict] = field(default_factory=list)
    fundamentado: bool = True
    motivo: str = ""


def _referencias_validas(cliente_id: str, df: pd.DataFrame) -> dict[str, float]:
    """Todo numero que seria legitimo citar sobre este cliente: os agregados do
    cliente inteiro, a soma de cada data e de cada canal com 2+ operacoes, e cada
    operacao individual - nesta ordem, de propósito (ver abaixo).

    Quando o cliente tem um numero impar de operacoes, a mediana e, por definicao,
    igual ao valor de uma operacao real do meio da distribuicao - nao coincidencia,
    matematica. Se essa operacao aparecesse primeiro no dict, verificar() atribuiria
    a ela (fonte "operacao X") uma citacao que na verdade e da mediana ("valor mediano
    de R$X, indicando..."), e o filtro de contexto (citado_como_atipico + fonte
    comeca com "operacao") dispararia um falso positivo de "atipico incorreto" -
    aconteceu de verdade com CLI-014 e CLI-005 (ambos com 11 operacoes) numa
    reexecucao do lote. Agregados vem primeiro para que, em caso de empate de valor,
    a citacao seja atribuida ao agregado - a leitura mais provavel quando o proprio
    texto diz "mediano"/"medio"/"total"."""
    sub = df[df["cliente_id"] == cliente_id]
    refs: dict[str, float] = {}

    refs["volume_total_cliente"] = round(float(sub["valor_brl"].sum()), 2)
    refs["media_cliente"] = round(float(sub["valor_brl"].mean()), 2)
    refs["mediana_cliente"] = round(float(sub["valor_brl"].median()), 2)

    com_data = sub[sub["data_valida"]]
    for data, grupo in com_data.groupby("data"):
        if len(grupo) >= 2:  # qualquer agrupamento por data que faca sentido citar
            refs[f"soma_do_dia_{data.strftime('%Y-%m-%d')}"] = round(
                float(grupo["valor_brl"].sum()), 2
            )

    # Idem para canal: perfil_canal() (uma das 3 ferramentas do agente) devolve volume
    # por canal, e o parecer pode citar essa soma ("R$X concentrado em duas operacoes
    # TED"). Sem esta referencia, um valor legitimo (mas nao individual nem agregado
    # do cliente inteiro) e reportado como "nao encontrado" - achado real em CLI-030.
    for canal, grupo in sub.groupby("canal"):
        if len(grupo) >= 2:
            refs[f"soma_canal_{canal}"] = round(float(grupo["valor_brl"].sum()), 2)

    for _, row in sub.iterrows():
        refs[f"operacao {row['id']}"] = round(float(row["valor_brl"]), 2)

    return refs


def verificar(cliente_id: str, justificativa: str, df: pd.DataFrame) -> Aderencia:
    """df ja deve estar limpo/com regras aplicadas (saida de aplicar_regras)."""
    referencias = _referencias_validas(cliente_id, df)

    extraidos = _extrair_com_posicao(justificativa)
    verificaveis = [e for e in extraidos if not e["e_limiar"]]
    citados = [e["valor"] for e in verificaveis]
    limiares = [e["valor"] for e in extraidos if e["e_limiar"]]

    resultado = Aderencia(
        cliente_id=cliente_id, valores_citados=citados, valores_limiar_ignorados=limiares
    )

    sub = df[df["cliente_id"] == cliente_id]
    valores_atipicos_reais = set(
        sub.loc[sub["flag_valor_atipico"], "valor_brl"].round(2)
    )

    classificacao: dict[int, tuple[str, str | None]] = {}  # trecho_inicio -> (classe, fonte)
    for item in verificaveis:
        v = item["valor"]
        tolerancia = item.get("tolerancia", TOLERANCIA_R)
        fonte = next(
            (nome for nome, ref in referencias.items() if abs(v - ref) <= tolerancia),
            None,
        )
        if not fonte:
            resultado.valores_nao_encontrados.append(v)
            classificacao[item["trecho_inicio"]] = ("nao_encontrado", None)
            continue

        resultado.valores_confirmados.append({"valor": v, "fonte": fonte})
        classificacao[item["trecho_inicio"]] = ("confirmado", fonte)

        # existe, mas foi citado como atipico sendo que nao e?
        if item["citado_como_atipico"] and fonte.startswith("operacao"):
            e_atipico_real = any(
                abs(v - real) <= tolerancia for real in valores_atipicos_reais
            )
            if not e_atipico_real:
                resultado.atipicos_incorretos.append({"valor": v, "fonte": fonte})
                classificacao[item["trecho_inicio"]] = ("atipico_incorreto", fonte)

    resultado.marcas = [
        {
            "inicio": e["trecho_inicio"],
            "fim": e["trecho_fim"],
            "valor": e["valor"],
            "classe": "limiar" if e["e_limiar"] else classificacao[e["trecho_inicio"]][0],
            "fonte": None if e["e_limiar"] else classificacao[e["trecho_inicio"]][1],
        }
        for e in extraidos
    ]

    if not citados:
        # Cliente sem NENHUMA flag deterministica (nem fracionamento, nem valor
        # atipico): a justificativa nao ter numero para citar e o esperado, nao uma
        # falha de fundamentacao - achado real ao cobrir os 30 clientes (6 dos "nao
        # fundamentados" eram na verdade clientes baixo risco descrevendo
        # corretamente a ausencia de anomalia, ex.: "sem indicadores de fracionamento
        # ou valores atipicos"). So marca como nao fundamentado quando o cliente TEM
        # flag e mesmo assim nao cita nada (esse caso continua sendo uma falha real).
        tem_flag = bool(sub["flag_fracionamento"].any() or sub["flag_valor_atipico"].any())
        if tem_flag:
            resultado.fundamentado = False
            resultado.motivo = "nenhum valor verificavel citado (so limiares, ou nenhum R$ no texto)"
        else:
            resultado.motivo = "cliente sem flags deterministicas - nada a fundamentar"
    elif resultado.valores_nao_encontrados:
        resultado.fundamentado = False
        resultado.motivo = (
            f"{len(resultado.valores_nao_encontrados)}/{len(citados)} valores citados "
            "nao correspondem a nenhuma operacao, soma diaria ou agregado do cliente"
        )
    elif resultado.atipicos_incorretos:
        resultado.fundamentado = False
        valores = [d["valor"] for d in resultado.atipicos_incorretos]
        resultado.motivo = (
            f"valores citados como atipicos que NAO tem flag_valor_atipico: {valores} "
            "- o numero existe na base, mas o parecer o usa no contexto errado"
        )
    else:
        resultado.motivo = f"{len(citados)}/{len(citados)} valores citados conferem"

    return resultado
