"""Fase 2 - a API da Mesa de Triagem.

A API NAO calcula nada. Toda regra de negocio ja existe e ja rodou: as regras
gravaram sinalizacoes e alertas (Fase 0), o worker gravou pareceres, evidencias
e aderencia (Fase 1). Aqui so existe contrato HTTP sobre o que esta no store.

Isso e uma restricao deliberada, nao economia de esforco. Se a API recalculasse
"qual operacao e atipica" para desenhar a tela, passaria a existir uma segunda
resposta para uma pergunta que o store ja respondeu - e a que ninguem audita e a
que diverge. As duas unicas derivacoes aqui (`concorda` e o nivel normalizado)
reusam a normalizacao de nivel_2/confronto.py em vez de reescreve-la.

Rodar:
    uvicorn mesa.api:app --reload          # http://127.0.0.1:8000/docs
"""
import base64
import binascii
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import mesa  # noqa: F401  - poe nivel_2/ no sys.path
from confronto import _normalizar_nivel
from mesa import contra_isca, db, metricas, repositorio

Estado = Literal["novo", "triado", "em_analise", "concluido"]
TipoDecisao = Literal["concordo", "discordo", "escalar"]
NIVEIS = ("baixo", "médio", "alto")  # o vocabulario do store, com acento (confronto.py)

# A tela da Fase 3 (HTML + JS puro, sem build) servida pela propria API: mesma
# origem, nenhum segundo servidor para subir, nenhum CORS para configurar.
WEB_DIR = Path(__file__).resolve().parent / "web"

# As transicoes que o POST /estado aceita. Tres, e so tres.
#
# Ficam de fora, de proposito:
#   novo -> triado          e do worker (mesa/triagem.py); a API nao duplica o
#                           caminho de quem roda o agente
#   em_analise -> concluido so pelo POST /decisao (Fase 4), que grava a decisao
#                           na mesma transacao. Por aqui, concluiria um caso sem
#                           registro do que o analista decidiu.
TRANSICOES = {
    ("novo", "em_analise"),     # analista pegou o caso antes da triagem
    ("triado", "em_analise"),   # analista pegou o caso triado
    ("em_analise", "triado"),   # analista devolveu o caso para a fila
}


# ============================================================================
# Contrato - os modelos SAO a especificacao que a tela (Fase 3) consome. O
# FastAPI valida a saida contra eles, entao um campo que some do SQL vira erro
# aqui, nao um `undefined` silencioso no navegador.
# ============================================================================


class Contagens(BaseModel):
    operacoes: int
    execucoes: int
    alertas: int
    pareceres: int


class Saude(BaseModel):
    status: Literal["ok"]
    versao_esquema: int
    execucao_atual: int | None
    contagens: Contagens


class ItemFila(BaseModel):
    alerta_id: int
    cliente_id: str
    estado: Estado
    origem: Literal["regra", "controle"]
    nivel_risco_regra: str
    # None = ainda nao ha parecer valido. NAO e o mesmo que "discorda": foi
    # exatamente essa confusao que o confronto.py teve que desfazer, separando
    # "respostas validas" de "concordancia" (docs/DECISOES.md).
    nivel_risco_agente: str | None
    concorda: bool | None
    fundamentado: bool | None
    total_sinalizacoes: int
    volume_total_brl: float
    qtd_operacoes: int
    analista_id: str | None
    criado_em: str
    # a decisao do analista, quando o caso ja foi concluido (Fase 4)
    decisao: TipoDecisao | None
    # Fase 5.1: o alerta anterior do cliente que este substituiu (chegou dado
    # novo), e o que o substituiu - None nos dois = unico alerta do cliente
    substitui_alerta_id: int | None
    substituido_por: int | None


class Fila(BaseModel):
    # None = a fila de trabalho: os alertas VIGENTES, de qualquer execucao
    execucao_id: int | None
    total: int
    itens: list[ItemFila]
    proximo_cursor: str | None


class Sinalizacao(BaseModel):
    regra: Literal["fracionamento", "valor_atipico"]
    operacao_id: str | None
    data: str | None
    detalhe: dict[str, Any]


class Parecer(BaseModel):
    parecer_id: int
    nivel_risco: str | None
    tipologia_suspeita: str | None
    red_flags: list[str]
    justificativa: str | None
    erro_parsing: str | None
    modelo: str
    versao_prompt: str
    origem_registro: str
    reaproveitado_de: int | None
    criado_em: str


class Marca(BaseModel):
    """Um valor em R$ citado na justificativa: onde esta (posicoes no texto) e
    como o verificador o classificou. A tela marca o trecho texto[inicio:fim]
    sem procurar numero nenhum por conta propria."""
    inicio: int
    fim: int
    valor: float
    classe: Literal["confirmado", "nao_encontrado", "atipico_incorreto", "limiar"]
    # "operacao OP-00269", "mediana_cliente", "soma_do_dia_2026-05-26",
    # "soma_canal_ted"... - o nome da referencia que conferiu, vindo do verificador
    fonte: str | None


class Aderencia(BaseModel):
    fundamentado: bool
    motivo: str
    valores_confirmados: list[Any]
    valores_nao_encontrados: list[Any]
    atipicos_incorretos: list[Any]
    marcas: list[Marca]


class Operacao(BaseModel):
    id: str
    data: str | None
    valor: float
    moeda: str
    valor_brl: float
    canal: str
    tipo: str
    contraparte: str
    # As duas marcas vem de `sinalizacoes`, gravadas quando as regras rodaram -
    # NAO de um recalculo aqui. E o que liga um numero do parecer a sua fonte.
    flag_valor_atipico: bool
    em_dia_de_fracionamento: bool


class VersaoParecer(BaseModel):
    parecer_id: int
    alerta_id: int | None
    nivel_risco: str | None
    origem_registro: str
    reaproveitado_de: int | None
    criado_em: str


class Decisao(BaseModel):
    decisao: TipoDecisao
    analista_id: str
    nivel_risco_analista: str | None
    # o nivel do parecer que o analista tinha na frente, gravado com a decisao
    nivel_risco_agente: str | None
    motivo: str | None
    parecer_id: int | None
    decidido_em: str


class TransicaoRegistrada(BaseModel):
    estado_anterior: Estado
    estado_novo: Estado
    ator: str
    ator_tipo: Literal["analista", "sistema"]
    registrado_em: str


class Componente(BaseModel):
    """Um termo do escore de uma ligacao, com a frase de onde saiu (Fase 6)."""
    nome: Literal["contraparte", "janela", "faixa", "distribuicao", "analise", "volume"]
    valor: float
    procedencia: str


class Ligacao(BaseModel):
    operacao_id: str
    cliente_id: str
    data: str | None
    valor_brl: float
    contraparte: str
    lote_id: int
    # A = na janela de dias; B = chegou durante a analise do caso
    padroes: list[Literal["A", "B"]]
    # o alerta vigente do cliente ligado (None se ele nao tem alerta)
    alerta_do_cliente: int | None = None
    # peso da contraparte (componentes[0]) x soma dos demais
    escore: float
    componentes: list[Componente]


class Suspeita(BaseModel):
    suspeita_id: int
    alerta_origem_id: int
    cliente_origem: str      # o caso de onde a caca partiu (a isca)
    cliente_id: str          # o cliente que a ligacao aponta
    operacoes: list[str]
    # o retrato do que a tela mostrava ao registrar, recalculado no servidor
    ligacoes: list[Ligacao]
    versao_caca: str
    analista_id: str
    motivo: str
    registrado_em: str


class PeriodoAnalise(BaseModel):
    inicio: str
    fim: str | None          # None = o caso continua em analise
    analista: str


class ContraIsca(BaseModel):
    alerta_id: int
    cliente_id: str
    execucao_id: int
    versao_caca: str
    parametros: dict[str, float]
    # os limites GRAVADOS na execucao do alerta, de onde sai a "faixa"
    limites: dict[str, float]
    periodos_em_analise: list[PeriodoAnalise]
    ligacoes: list[Ligacao]
    # as suspeitas ja registradas a partir deste caso - a tela marca a ligacao
    suspeitas: list[Suspeita]


class Caso(BaseModel):
    alerta: ItemFila
    execucao_id: int
    sinalizacoes: list[Sinalizacao]
    parecer: Parecer | None
    aderencia: Aderencia | None
    operacoes: list[Operacao]
    # O append-only ficando VISIVEL: se este cliente ja recebeu outro parecer,
    # o analista ve. E a razao de a Fase 1 existir.
    historico_parecer: list[VersaoParecer]
    # Para onde este caso pode ir a partir do estado atual, derivado da MESMA
    # tabela TRANSICOES que o POST aplica. A tela mostra so estes botoes em vez
    # de manter uma segunda copia da maquina de estados em JavaScript.
    transicoes_permitidas: list[Estado]
    # Fase 4: o que o analista decidiu (null ate o caso ser concluido) e o
    # caminho do caso ate aqui, em ordem. A trilha comeca na migracao para o
    # esquema v6 - transicoes anteriores nao foram gravadas e nao sao inventadas.
    decisao: Decisao | None
    trilha: list[TransicaoRegistrada]
    # Fase 5.1: a decisao mais recente tomada num alerta ANTERIOR deste cliente
    # (o que este substituiu, ou antes). O analista ve o que ja se decidiu sobre
    # o cliente antes de o dado novo chegar - mas decide de novo, sobre o novo.
    decisao_anterior: Decisao | None
    # Fase 6: suspeitas registradas A PARTIR deste caso (a caca partiu dele) e
    # SOBRE este cliente (a caca de outro caso apontou para ele). A segunda e
    # a que muda o trabalho: o analista abre um caso "sem sinal" e ve que
    # alguem ja o ligou a uma isca.
    suspeitas_registradas: list[Suspeita]
    suspeitas_sobre_o_cliente: list[Suspeita]


class Evidencia(BaseModel):
    ordem: int
    tool: str
    args: dict[str, Any]
    payload: Any


class NovoEstado(BaseModel):
    estado: Estado


class Transicao(BaseModel):
    alerta_id: int
    estado_anterior: Estado
    estado: Estado
    analista_id: str | None


# Limites de tamanho: decisao e trilha sao append-only - o que entra fica para
# sempre. A tela ja limitava o motivo a 2000; a API passou a limitar tambem
# (achado da auditoria: ela aceitava qualquer tamanho).
MAX_MOTIVO = 2000
MAX_ANALISTA = 100


class NovaDecisao(BaseModel):
    decisao: TipoDecisao
    nivel_risco: str | None = Field(None, max_length=20)
    motivo: str | None = Field(None, max_length=MAX_MOTIVO)
    # O parecer que a tela MOSTROU ao analista. Obrigatorio mesmo quando null
    # (caso sem parecer): o cliente tem que declarar o que viu, nao deixar a API
    # supor. Se o parecer atual do alerta for outro, a decisao e recusada.
    parecer_id: int | None


class NovaSuspeita(BaseModel):
    cliente_id: str = Field(..., min_length=1, max_length=50)
    # as operacoes da ligacao que o analista aponta - conferidas contra a caca
    # recalculada no servidor, nunca aceitas como vieram
    operacoes: list[str] = Field(..., min_length=1, max_length=100)
    motivo: str = Field(..., max_length=MAX_MOTIVO)


Matriz = dict[str, dict[str, int]]


class Comparacao(BaseModel):
    comparaveis: int
    # None quando nao ha par comparavel - nunca 0.0 (ver mesa/metricas.py)
    concordancia: float | None
    matriz: Matriz


class AgenteVsAnalista(Comparacao):
    decididos_com_parecer: int
    aceitacao_do_parecer: float | None


class GrupoAderencia(BaseModel):
    decididos: int
    concordo: int
    discordo: int
    escalar: int
    taxa_de_rejeicao: float | None


class AderenciaVsDecisao(BaseModel):
    fundamentado: GrupoAderencia
    nao_fundamentado: GrupoAderencia
    sem_verificacao: GrupoAderencia


class TempoPorDecisao(BaseModel):
    casos: int
    mediana_s: float | None


class LinhaDeBase(BaseModel):
    minutos: float
    procedencia: str


class Tempo(BaseModel):
    casos_medidos: int
    casos_sem_trilha_completa: int
    mediana_s: float | None
    media_s: float | None
    por_decisao: dict[str, TempoPorDecisao]
    linha_de_base: LinhaDeBase | None
    economia_mediana_s: float | None


class Metricas(BaseModel):
    execucao_id: int | None
    casos: int
    decididos: int
    por_decisao: dict[str, int]
    agente_vs_analista: AgenteVsAnalista
    regra_vs_analista: Comparacao
    regra_vs_agente: Comparacao
    aderencia_vs_decisao: AderenciaVsDecisao
    tempo: Tempo


class Execucao(BaseModel):
    id: int
    lote_id: int
    versao_regras: str
    parametros: dict[str, Any]
    executado_em: str
    operacoes_avaliadas: int
    clientes_fracionamento: int
    operacoes_atipicas: int
    alertas_regra: int
    alertas_controle: int


# ============================================================================
# App e conexao
# ============================================================================

app = FastAPI(
    title="Mesa de Triagem PLD",
    version="4.0",
    description="Leitura do store da Mesa e transicao de estado dos casos. "
                "Nenhum calculo de regra acontece aqui.",
)

# A tela da Fase 3 roda em localhost numa porta qualquer. Nenhuma origem fora da
# maquina: a API nao tem autenticacao (ver X-Analista) e nao pode ser alcancavel
# de outro lugar enquanto isso for verdade.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Analista"],
)


def conexao():
    """Uma conexao por requisicao, aberta e fechada aqui.

    O caminho e lido de `db.CAMINHO_PADRAO` NA HORA, nao na importacao - e o que
    deixa os testes apontarem a API para um banco temporario.

    Duas recusas antes de abrir, as duas com 503 (o servico existe, o dado nao):
      - store inexistente: `conectar()` criaria um banco VAZIO em silencio, e a
        tela mostraria "0 casos" como se fosse verdade
      - esquema de outra versao: ler colunas que nao existem daria 500 opaco
    Uma leitura nao pode escrever, entao a versao e CONFERIDA, nao aplicada.
    """
    caminho = Path(db.CAMINHO_PADRAO)
    if not caminho.exists():
        raise HTTPException(
            503,
            f"store não encontrado em {caminho} - rode: python -m mesa.ingestao "
            "&& python -m mesa.regras_run && python -m mesa.triagem",
        )

    conn = db.conectar(caminho, criar_esquema=False, check_same_thread=False)
    try:
        versao = conn.execute("PRAGMA user_version").fetchone()[0]
        if versao != db.VERSAO_ESQUEMA:
            raise HTTPException(
                503,
                f"store na versão de esquema {versao}, a API espera "
                f"{db.VERSAO_ESQUEMA} - rode: python -m mesa.db (migra versões "
                "aditivas; as demais exigem reconstruir o store)",
            )
        yield conn
    finally:
        conn.close()


def _execucao(conn: sqlite3.Connection, execucao_id: int | None) -> int:
    if execucao_id is not None:
        if conn.execute("SELECT 1 FROM execucoes_regras WHERE id = ?", (execucao_id,)).fetchone():
            return execucao_id
        raise HTTPException(404, f"execução {execucao_id} não existe")

    linha = conn.execute("SELECT MAX(id) FROM execucoes_regras").fetchone()
    if linha[0] is None:
        raise HTTPException(503, "store sem execução de regras - rode: python -m mesa.regras_run")
    return int(linha[0])


# O parecer ATUAL de um alerta e sempre o mais recente - o log e append-only, e
# "atual" e uma consulta, nao uma coluna. Mesmo criterio de mesa/pareceres.py.
_PARECER_ATUAL = """
    SELECT id FROM pareceres WHERE alerta_id = a.id
    ORDER BY criado_em DESC, id DESC LIMIT 1
"""

_SELECT_ALERTA = f"""
    SELECT a.id AS alerta_id, a.cliente_id, a.estado, a.origem, a.nivel_risco_regra,
           a.total_sinalizacoes, a.volume_total_brl, a.qtd_operacoes,
           a.analista_id, a.criado_em, a.execucao_id,
           p.nivel_risco AS nivel_risco_agente, ad.fundamentado,
           d.decisao, a.substitui_alerta_id,
           (SELECT s.id FROM alertas s WHERE s.substitui_alerta_id = a.id) AS substituido_por
    FROM alertas a
    LEFT JOIN pareceres p ON p.id = ({_PARECER_ATUAL})
    LEFT JOIN aderencia ad ON ad.parecer_id = p.id
    LEFT JOIN decisoes d ON d.alerta_id = a.id
"""

# Mesma ordem de dados.ranking_clientes_sinalizados(). cliente_id desempata
# porque SQL nao garante estabilidade de sort onde o pandas garante - uma fila
# que reordena entre duas leituras e ruim para o analista.
_ORDEM_FILA = "ORDER BY a.total_sinalizacoes DESC, a.volume_total_brl DESC, a.cliente_id ASC"


def _item_fila(linha: sqlite3.Row) -> ItemFila:
    agente = _normalizar_nivel(linha["nivel_risco_agente"])
    return ItemFila(
        alerta_id=linha["alerta_id"],
        cliente_id=linha["cliente_id"],
        estado=linha["estado"],
        origem=linha["origem"],
        nivel_risco_regra=linha["nivel_risco_regra"],
        nivel_risco_agente=agente,
        concorda=None if agente is None else agente == linha["nivel_risco_regra"],
        fundamentado=None if linha["fundamentado"] is None else bool(linha["fundamentado"]),
        total_sinalizacoes=linha["total_sinalizacoes"],
        volume_total_brl=linha["volume_total_brl"],
        qtd_operacoes=linha["qtd_operacoes"],
        analista_id=linha["analista_id"],
        criado_em=linha["criado_em"],
        decisao=linha["decisao"],
        substitui_alerta_id=linha["substitui_alerta_id"],
        substituido_por=linha["substituido_por"],
    )


def _alerta_ou_404(conn: sqlite3.Connection, alerta_id: int) -> sqlite3.Row:
    linha = conn.execute(f"{_SELECT_ALERTA} WHERE a.id = ?", (alerta_id,)).fetchone()
    if linha is None:
        raise HTTPException(404, f"alerta {alerta_id} não existe")
    return linha


def _lote_da_execucao(conn: sqlite3.Connection, execucao_id: int) -> int:
    return conn.execute(
        "SELECT lote_id FROM execucoes_regras WHERE id = ?", (execucao_id,)
    ).fetchone()[0]


def _decisao_de(conn: sqlite3.Connection, alerta_id: int) -> "Decisao | None":
    d = conn.execute("SELECT * FROM decisoes WHERE alerta_id = ?", (alerta_id,)).fetchone()
    return None if d is None else Decisao(
        decisao=d["decisao"], analista_id=d["analista_id"],
        nivel_risco_analista=d["nivel_risco_analista"], nivel_risco_agente=d["nivel_risco_agente"],
        motivo=d["motivo"], parecer_id=d["parecer_id"], decidido_em=d["decidido_em"],
    )


def _permitidas(alerta: sqlite3.Row) -> list[str]:
    """As transicoes do POST /estado a partir do estado atual. Um alerta
    SUBSTITUIDO (chegou dado novo, ha um alerta vigente para o cliente) so pode
    ser devolvido - pega-lo seria trabalhar sobre a base velha."""
    permitidas = sorted(d for o, d in TRANSICOES if o == alerta["estado"])
    if alerta["substituido_por"] is not None:
        permitidas = [d for d in permitidas if d != "em_analise"]
    return permitidas


def _recusar_se_substituido(alerta: sqlite3.Row, acao: str) -> None:
    if alerta["substituido_por"] is not None:
        raise HTTPException(
            409,
            f"o alerta {alerta['alerta_id']} do caso {alerta['cliente_id']} foi substituído "
            f"pelo alerta {alerta['substituido_por']} (chegaram operações novas) - "
            f"{acao} no caso atual",
        )


def _parecer_atual_id(conn: sqlite3.Connection, alerta_id: int) -> int | None:
    linha = conn.execute(
        "SELECT id FROM pareceres WHERE alerta_id = ? ORDER BY criado_em DESC, id DESC LIMIT 1",
        (alerta_id,),
    ).fetchone()
    return int(linha[0]) if linha else None


# ---------- cursor da fila ----------
#
# Paginacao por CHAVE (a ultima posicao vista), nao por offset. A fila muda
# enquanto e lida - um analista pega um caso, o estado muda, o caso sai do
# filtro - e offset pularia ou repetiria itens quando isso acontece entre uma
# pagina e outra. A chave e a propria tripla de ordenacao.


def _codificar_cursor(linha: sqlite3.Row) -> str:
    chave = [linha["total_sinalizacoes"], linha["volume_total_brl"], linha["cliente_id"]]
    return base64.urlsafe_b64encode(json.dumps(chave).encode()).decode()


def _decodificar_cursor(cursor: str) -> tuple[int, float, str]:
    try:
        total, volume, cliente = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return int(total), float(volume), str(cliente)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise HTTPException(400, "cursor inválido - use o proximo_cursor devolvido pela própria /fila")


# ============================================================================
# Endpoints
# ============================================================================


@app.get("/saude", response_model=Saude)
def saude(conn: sqlite3.Connection = Depends(conexao)):
    """Prova de que a API esta lendo o store, e qual."""
    conta = lambda tabela: conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]  # noqa: E731
    atual = conn.execute("SELECT MAX(id) FROM execucoes_regras").fetchone()[0]
    return Saude(
        status="ok",
        versao_esquema=conn.execute("PRAGMA user_version").fetchone()[0],
        execucao_atual=atual,
        contagens=Contagens(
            operacoes=conta("operacoes"),
            execucoes=conta("execucoes_regras"),
            alertas=conta("alertas"),
            pareceres=conta("pareceres"),
        ),
    )


@app.get("/fila", response_model=Fila)
def fila(
    estado: Estado | None = None,
    origem: Literal["regra", "controle", "todos"] = "regra",
    execucao_id: int | None = None,
    limite: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    conn: sqlite3.Connection = Depends(conexao),
):
    """A fila do analista, na ordem do ranking das regras: por padrao, o alerta
    vigente de cada cliente; com `execucao_id`, os alertas daquela execucao.

    `origem=regra` por padrao: a fila de trabalho nao mostra cliente sem
    sinalizacao. Os de controle existem para medir falso negativo, nao para
    ocupar o analista - `origem=todos` os inclui.
    """
    # Sem execucao_id: os alertas VIGENTES (Fase 5.1). "A execucao mais recente"
    # deixou de ser o retrato completo quando a execucao passou a poder ser
    # incremental - ela pode ter so os 2 clientes que mudaram.
    if execucao_id is None:
        _execucao(conn, None)  # 503 se o store nao tem execucao nenhuma
        filtros, params = [repositorio.VIGENTE], []
    else:
        execucao_id = _execucao(conn, execucao_id)
        filtros, params = ["a.execucao_id = ?"], [execucao_id]
    if estado is not None:
        filtros.append("a.estado = ?")
        params.append(estado)
    if origem != "todos":
        filtros.append("a.origem = ?")
        params.append(origem)

    total = conn.execute(
        f"SELECT COUNT(*) FROM alertas a WHERE {' AND '.join(filtros)}", params
    ).fetchone()[0]

    if cursor is not None:
        t, v, c = _decodificar_cursor(cursor)
        # igualdade de float e segura aqui: o valor do cursor veio do proprio
        # banco e passou por JSON, cujo repr de float faz ida-e-volta exata
        filtros.append(
            "(a.total_sinalizacoes < ? OR (a.total_sinalizacoes = ? AND "
            "(a.volume_total_brl < ? OR (a.volume_total_brl = ? AND a.cliente_id > ?))))"
        )
        params.extend([t, t, v, v, c])

    linhas = conn.execute(
        f"{_SELECT_ALERTA} WHERE {' AND '.join(filtros)} {_ORDEM_FILA} LIMIT ?",
        [*params, limite + 1],  # +1 para saber se ha proxima pagina sem outra query
    ).fetchall()

    pagina, sobrou = linhas[:limite], len(linhas) > limite
    return Fila(
        execucao_id=execucao_id,
        total=total,
        itens=[_item_fila(l) for l in pagina],
        proximo_cursor=_codificar_cursor(pagina[-1]) if sobrou else None,
    )


@app.get("/alertas/{alerta_id}", response_model=Caso)
def caso(alerta_id: int, conn: sqlite3.Connection = Depends(conexao)):
    """O caso completo numa requisicao so - o que a tela da Fase 3 precisa."""
    alerta = _alerta_ou_404(conn, alerta_id)
    execucao_id, cliente_id = alerta["execucao_id"], alerta["cliente_id"]

    sinalizacoes = [
        Sinalizacao(
            regra=s["regra"], operacao_id=s["operacao_id"], data=s["data"],
            detalhe=json.loads(s["detalhe_json"]),
        )
        for s in conn.execute(
            "SELECT regra, operacao_id, data, detalhe_json FROM sinalizacoes "
            "WHERE execucao_id = ? AND cliente_id = ? ORDER BY regra, data, operacao_id",
            (execucao_id, cliente_id),
        )
    ]
    atipicas = {s.operacao_id for s in sinalizacoes if s.regra == "valor_atipico"}
    dias_frac = {s.data for s in sinalizacoes if s.regra == "fracionamento"}

    operacoes = [
        Operacao(
            id=o["id"], data=o["data"], valor=o["valor"], moeda=o["moeda"],
            valor_brl=o["valor_brl"], canal=o["canal"], tipo=o["tipo"],
            contraparte=o["contraparte"],
            flag_valor_atipico=o["id"] in atipicas,
            em_dia_de_fracionamento=o["data"] in dias_frac,
        )
        # A base COMO ERA no lote em que as regras rodaram para este alerta, nao
        # a de hoje (Fase 5.2): um caso decidido ontem nao pode aparecer com uma
        # operacao que chegou - ou foi corrigida - depois. Sem data vai para o fim.
        for o in repositorio.operacoes_no_lote(conn, _lote_da_execucao(conn, execucao_id), cliente_id)
    ]

    parecer, aderencia = None, None
    parecer_id = _parecer_atual_id(conn, alerta_id)
    if parecer_id is not None:
        p = conn.execute("SELECT * FROM pareceres WHERE id = ?", (parecer_id,)).fetchone()
        parecer = Parecer(
            parecer_id=p["id"],
            nivel_risco=_normalizar_nivel(p["nivel_risco"]),
            tipologia_suspeita=p["tipologia_suspeita"],
            red_flags=json.loads(p["red_flags_json"]) if p["red_flags_json"] else [],
            justificativa=p["justificativa"],
            erro_parsing=p["erro_parsing"],
            modelo=p["modelo"],
            versao_prompt=p["versao_prompt"],
            origem_registro=p["origem_registro"],
            reaproveitado_de=p["reaproveitado_de"],
            criado_em=p["criado_em"],
        )
        a = conn.execute("SELECT * FROM aderencia WHERE parecer_id = ?", (parecer_id,)).fetchone()
        if a is not None:
            aderencia = Aderencia(
                fundamentado=bool(a["fundamentado"]),
                motivo=a["motivo"],
                valores_confirmados=json.loads(a["valores_confirmados_json"]),
                valores_nao_encontrados=json.loads(a["valores_nao_encontrados_json"]),
                atipicos_incorretos=json.loads(a["atipicos_incorretos_json"]),
                marcas=json.loads(a["marcas_json"]),
            )

    historico = [
        VersaoParecer(
            parecer_id=h["id"], alerta_id=h["alerta_id"],
            nivel_risco=_normalizar_nivel(h["nivel_risco"]),
            origem_registro=h["origem_registro"], reaproveitado_de=h["reaproveitado_de"],
            criado_em=h["criado_em"],
        )
        for h in conn.execute(
            "SELECT id, alerta_id, nivel_risco, origem_registro, reaproveitado_de, criado_em "
            "FROM pareceres WHERE cliente_id = ? ORDER BY criado_em DESC, id DESC",
            (cliente_id,),
        )
    ]

    decisao = _decisao_de(conn, alerta_id)
    decisao_anterior = None
    anterior = alerta["substitui_alerta_id"]
    while anterior is not None and decisao_anterior is None:
        decisao_anterior = _decisao_de(conn, anterior)
        anterior = conn.execute(
            "SELECT substitui_alerta_id FROM alertas WHERE id = ?", (anterior,)
        ).fetchone()[0]
    trilha = [
        TransicaoRegistrada(
            estado_anterior=r["estado_anterior"], estado_novo=r["estado_novo"],
            ator=r["ator"], ator_tipo=r["ator_tipo"], registrado_em=r["registrado_em"],
        )
        for r in conn.execute(
            "SELECT * FROM transicoes WHERE alerta_id = ? ORDER BY id", (alerta_id,)
        )
    ]

    return Caso(
        alerta=_item_fila(alerta),
        execucao_id=execucao_id,
        sinalizacoes=sinalizacoes,
        parecer=parecer,
        aderencia=aderencia,
        operacoes=operacoes,
        historico_parecer=historico,
        transicoes_permitidas=_permitidas(alerta),
        decisao=decisao,
        trilha=trilha,
        decisao_anterior=decisao_anterior,
        suspeitas_registradas=_suspeitas(conn, "s.alerta_origem_id = ?", alerta_id),
        suspeitas_sobre_o_cliente=_suspeitas(conn, "s.cliente_id = ?", cliente_id),
    )


@app.get("/alertas/{alerta_id}/evidencias", response_model=list[Evidencia])
def evidencias(alerta_id: int, conn: sqlite3.Connection = Depends(conexao)):
    """O que o agente VIU quando decidiu - o retorno de cada ferramenta.

    Alerta sem parecer devolve lista vazia, nao 404: o alerta existe, so ainda
    nao foi triado. 404 fica reservado para o que nao existe.
    """
    _alerta_ou_404(conn, alerta_id)
    parecer_id = _parecer_atual_id(conn, alerta_id)
    if parecer_id is None:
        return []
    return [
        Evidencia(
            ordem=e["ordem"], tool=e["tool"],
            args=json.loads(e["args_json"]), payload=json.loads(e["payload_json"]),
        )
        for e in conn.execute(
            "SELECT ordem, tool, args_json, payload_json FROM evidencias "
            "WHERE parecer_id = ? ORDER BY ordem",
            (parecer_id,),
        )
    ]


@app.post("/alertas/{alerta_id}/estado", response_model=Transicao)
def mudar_estado(
    alerta_id: int,
    corpo: NovoEstado,
    x_analista: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(conexao),
):
    """Transicao de estado de um caso.

    Quem pede vem no header X-Analista (passo 2.5) - e so no header: o corpo
    carrega apenas o estado de destino. Duas fontes para o mesmo fato e como
    duas verdades comecam.
    """
    analista = _analista(x_analista, "mudar o estado de um caso")

    alerta = _alerta_ou_404(conn, alerta_id)
    atual, dono = alerta["estado"], alerta["analista_id"]
    destino = corpo.estado
    # nas mensagens, o caso pelo nome que o analista conhece (CLI-028), nao
    # pelo id interno do alerta
    caso_nome = alerta["cliente_id"]

    if destino == "em_analise":
        _recusar_se_substituido(alerta, "pegue o caso")

    # Caso ja pego por outro: e o conflito mais comum (tela desatualizada), e
    # merece dizer QUEM pegou, nao "transicao em_analise -> em_analise".
    if atual == "em_analise" and destino == "em_analise":
        quem = "você" if dono == analista else dono
        raise HTTPException(409, f"o caso {caso_nome} já está em análise com {quem}")

    if (atual, destino) not in TRANSICOES:
        permitidas = sorted(d for o, d in TRANSICOES if o == atual)
        motivo = (
            f"concluir um caso exige a decisão do analista registrada junto - "
            f"use POST /alertas/{alerta_id}/decisao"
            if destino == "concluido" else
            f"a partir de '{atual}' só é permitido: {permitidas or 'nenhuma transição'}"
        )
        raise HTTPException(409, f"transição '{atual}' -> '{destino}' não permitida: {motivo}")

    # So quem pegou pode devolver. Sem esta regra, qualquer analista com a
    # pagina aberta devolveria para a fila um caso em analise por outro, e o dono
    # nem ficaria sabendo. (Nao e controle de acesso - X-Analista e so
    # identificacao, ver 2.5 -, mas impede a interferencia por engano. Reatribuir
    # caso de outro e papel de supervisor, que depende de autenticacao de verdade.)
    if atual == "em_analise" and destino == "triado" and dono and dono != analista:
        raise HTTPException(
            409, f"o caso {caso_nome} está em análise com {dono}; só quem pegou o caso pode devolvê-lo"
        )

    # Pegar o caso grava quem pegou; devolver para a fila libera o dono.
    novo_analista = analista if destino == "em_analise" else None

    # Compare-and-set: so atualiza se o estado AINDA for o que acabamos de ler.
    # Ler-e-depois-escrever deixaria dois analistas pegarem o mesmo caso ao mesmo
    # tempo; com a condicao no WHERE, o banco garante que so um vence.
    # O dono lido tambem entra na condicao (`IS` compara NULL com seguranca):
    # se outro analista pegou e devolveu o caso entre a leitura e a escrita, o
    # estado voltou ao mesmo, mas o caso ja nao e o que foi lido.
    # A transicao entra na trilha na MESMA transacao: ou as duas coisas ficam
    # gravadas, ou nenhuma.
    with conn:
        cur = conn.execute(
            "UPDATE alertas SET estado = ?, analista_id = ? "
            "WHERE id = ? AND estado = ? AND analista_id IS ?",
            (destino, novo_analista, alerta_id, atual, dono),
        )
        if cur.rowcount == 1:
            _registrar_transicao(conn, alerta_id, atual, destino, analista)
    if cur.rowcount == 0:
        raise HTTPException(
            409, f"o caso {caso_nome} mudou de estado durante a requisição - recarregue e tente de novo"
        )

    return Transicao(
        alerta_id=alerta_id, estado_anterior=atual, estado=destino, analista_id=novo_analista,
    )


def _analista(x_analista: str | None, para_que: str) -> str:
    analista = (x_analista or "").strip()
    if not analista:
        raise HTTPException(400, f"header X-Analista obrigatório para {para_que}")
    if len(analista) > MAX_ANALISTA:
        raise HTTPException(400, f"header X-Analista com mais de {MAX_ANALISTA} caracteres")
    return analista


def _registrar_transicao(conn: sqlite3.Connection, alerta_id: int, de: str, para: str,
                         analista: str) -> None:
    """Uma linha na trilha. Chamada DENTRO da transacao de quem mudou o estado -
    uma funcao so, para o formato da trilha nao divergir entre as rotas."""
    conn.execute(
        "INSERT INTO transicoes (alerta_id, estado_anterior, estado_novo, ator, ator_tipo, "
        "registrado_em) VALUES (?, ?, ?, ?, 'analista', ?)",
        (alerta_id, de, para, analista, db.agora_utc()),
    )


def _gravar_decisao(conn: sqlite3.Connection, alerta_id: int, parecer_id: int | None,
                    analista: str, corpo: NovaDecisao, nivel_analista: str | None,
                    nivel_agente: str | None, motivo: str | None, decidido_em: str) -> None:
    conn.execute(
        "INSERT INTO decisoes (alerta_id, parecer_id, analista_id, decisao, "
        "nivel_risco_analista, nivel_risco_agente, motivo, decidido_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (alerta_id, parecer_id, analista, corpo.decisao, nivel_analista, nivel_agente,
         motivo, decidido_em),
    )


@app.post("/alertas/{alerta_id}/decisao", response_model=Decisao)
def decidir(
    alerta_id: int,
    corpo: NovaDecisao,
    x_analista: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(conexao),
):
    """A decisao do analista - e a UNICA porta para `concluido`.

    Tres escritas numa transacao so: o caso sai de em_analise para concluido
    (compare-and-set, como no /estado), a decisao e gravada, e a transicao entra
    na trilha. Se qualquer uma falhar, nenhuma fica: nao existe caso concluido
    sem decisao, nem decisao de caso que continua aberto.

    Regras de cada decisao (tambem no schema - ver esquema.sql):
      concordo : exige parecer com nivel; o nivel gravado e o do parecer
      discordo : exige nivel_risco (o que o analista atribui) e motivo
      escalar  : exige motivo; nivel_risco opcional
    """
    analista = _analista(x_analista, "registrar uma decisão")

    # --- o corpo, sozinho, faz sentido? (422: o pedido esta mal formado) ---
    motivo = (corpo.motivo or "").strip() or None
    nivel = None
    if corpo.nivel_risco is not None:
        nivel = _normalizar_nivel(corpo.nivel_risco)
        if nivel not in NIVEIS:
            raise HTTPException(422, f"nível de risco '{corpo.nivel_risco}' inválido - use: {', '.join(NIVEIS)}")
    if corpo.decisao == "discordo" and (nivel is None or motivo is None):
        raise HTTPException(422, "discordar exige o nível de risco que você atribui e o motivo")
    if corpo.decisao == "escalar" and motivo is None:
        raise HTTPException(422, "escalar exige o motivo")

    # --- o caso esta num estado em que ESTE analista pode decidir? (409) ---
    alerta = _alerta_ou_404(conn, alerta_id)
    caso_nome, estado, dono = alerta["cliente_id"], alerta["estado"], alerta["analista_id"]
    if estado == "concluido":
        raise HTTPException(409, f"o caso {caso_nome} já foi decidido")
    _recusar_se_substituido(alerta, "decida")
    if estado != "em_analise":
        raise HTTPException(
            409, f"o caso {caso_nome} está '{estado}' - pegue o caso antes de decidir"
        )
    if dono != analista:
        raise HTTPException(
            409, f"o caso {caso_nome} está em análise com {dono}; só quem pegou o caso decide"
        )

    parecer_atual = _parecer_atual_id(conn, alerta_id)
    if corpo.parecer_id != parecer_atual:
        raise HTTPException(
            409,
            f"o parecer do caso {caso_nome} mudou desde que você o abriu - recarregue, "
            "leia o parecer atual e decida de novo",
        )

    nivel_agente = None
    if parecer_atual is not None:
        bruto = conn.execute(
            "SELECT nivel_risco FROM pareceres WHERE id = ?", (parecer_atual,)
        ).fetchone()[0]
        nivel_agente = _normalizar_nivel(bruto)
        nivel_agente = nivel_agente if nivel_agente in NIVEIS else None

    # --- concordar depende do parecer (422: nao ha com que concordar) ---
    if corpo.decisao == "concordo":
        if nivel_agente is None:
            raise HTTPException(
                422, "não há nível de risco do agente com que concordar - discorde ou escale"
            )
        if nivel is not None and nivel != nivel_agente:
            raise HTTPException(
                422, f"concordar grava o nível do agente ('{nivel_agente}'); para atribuir "
                     f"'{nivel}', registre 'discordo' com o motivo"
            )
        nivel = nivel_agente

    decidido_em = db.agora_utc()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE alertas SET estado = 'concluido' "
                "WHERE id = ? AND estado = 'em_analise' AND analista_id = ?",
                (alerta_id, analista),
            )
            if cur.rowcount == 0:
                raise _CasoMudou()
            _gravar_decisao(conn, alerta_id, parecer_atual, analista, corpo, nivel,
                            nivel_agente, motivo, decidido_em)
            _registrar_transicao(conn, alerta_id, "em_analise", "concluido", analista)
    except _CasoMudou:
        raise HTTPException(
            409, f"o caso {caso_nome} mudou de estado durante a requisição - recarregue e tente de novo"
        )

    return Decisao(
        decisao=corpo.decisao, analista_id=analista, nivel_risco_analista=nivel,
        nivel_risco_agente=nivel_agente, motivo=motivo, parecer_id=parecer_atual,
        decidido_em=decidido_em,
    )


class _CasoMudou(Exception):
    """O compare-and-set nao encontrou o caso como foi lido - desfaz a transacao."""


_SELECT_EXECUCAO = """
    SELECT e.*,
           (SELECT COUNT(*) FROM alertas WHERE execucao_id = e.id AND origem = 'regra') AS alertas_regra,
           (SELECT COUNT(*) FROM alertas WHERE execucao_id = e.id AND origem = 'controle') AS alertas_controle
    FROM execucoes_regras e
"""


def _execucao_modelo(linha: sqlite3.Row) -> Execucao:
    return Execucao(
        id=linha["id"], lote_id=linha["lote_id"], versao_regras=linha["versao_regras"],
        # os parametros GRAVADOS naquela execucao - nao os de dados.py hoje. E a
        # resposta para "com que limiar este alerta nasceu?"
        parametros=json.loads(linha["parametros_json"]),
        executado_em=linha["executado_em"],
        operacoes_avaliadas=linha["operacoes_avaliadas"],
        clientes_fracionamento=linha["clientes_fracionamento"],
        operacoes_atipicas=linha["operacoes_atipicas"],
        alertas_regra=linha["alertas_regra"],
        alertas_controle=linha["alertas_controle"],
    )


@app.get("/execucoes", response_model=list[Execucao])
def execucoes(conn: sqlite3.Connection = Depends(conexao)):
    return [_execucao_modelo(l) for l in conn.execute(f"{_SELECT_EXECUCAO} ORDER BY e.id DESC")]


@app.get("/execucoes/{execucao_id}", response_model=Execucao)
def execucao(execucao_id: int, conn: sqlite3.Connection = Depends(conexao)):
    linha = conn.execute(f"{_SELECT_EXECUCAO} WHERE e.id = ?", (execucao_id,)).fetchone()
    if linha is None:
        raise HTTPException(404, f"execução {execucao_id} não existe")
    return _execucao_modelo(linha)


# ============================================================================
# Fase 6 - contra-isca
# ============================================================================


def _suspeitas(conn: sqlite3.Connection, filtro: str, valor) -> list[Suspeita]:
    return [
        Suspeita(
            suspeita_id=r["id"], alerta_origem_id=r["alerta_origem_id"],
            cliente_origem=r["cliente_origem"], cliente_id=r["cliente_id"],
            operacoes=json.loads(r["operacoes_json"]), ligacoes=json.loads(r["ligacoes_json"]),
            versao_caca=r["versao_caca"], analista_id=r["analista_id"], motivo=r["motivo"],
            registrado_em=r["registrado_em"],
        )
        for r in conn.execute(
            "SELECT s.*, a.cliente_id AS cliente_origem FROM suspeitas s "
            f"JOIN alertas a ON a.id = s.alerta_origem_id WHERE {filtro} ORDER BY s.id",
            (valor,),
        )
    ]


@app.get("/alertas/{alerta_id}/contra-isca", response_model=ContraIsca)
def cacar(alerta_id: int, conn: sqlite3.Connection = Depends(conexao)):
    """O que passou ao lado deste caso: operacoes de OUTROS clientes ligadas a
    ele por contraparte, na janela de dias (A) ou chegadas durante a analise
    (B). Cada numero com a sua procedencia.

    A excecao declarada ao "a API nao calcula nada": a caca e uma CONSULTA sob
    demanda, nao uma regra - nao existe resultado gravado para ler. O calculo
    vive em mesa/contra_isca.py, deterministico e testado contra um cenario
    plantado com gabarito; a API so o expoe. E nao escreve nada."""
    _alerta_ou_404(conn, alerta_id)
    resultado = contra_isca.cacar(conn, alerta_id)
    return ContraIsca(**resultado, suspeitas=_suspeitas(conn, "s.alerta_origem_id = ?", alerta_id))


@app.post("/alertas/{alerta_id}/suspeitas", response_model=Suspeita, status_code=201)
def registrar_suspeita(
    alerta_id: int,
    corpo: NovaSuspeita,
    x_analista: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(conexao),
):
    """O analista assina uma suspeita a partir de uma ligacao da caca.

    O servidor RECALCULA a caca e confere que as operacoes apontadas sao
    ligacoes atuais daquele cliente - o retrato gravado (escores, frases) sai
    dali, nao do corpo da requisicao. Uma tela velha (a base cresceu e a
    ligacao mudou) recebe 409, nao grava um retrato que ja nao e verdade."""
    analista = _analista(x_analista, "registrar uma suspeita")
    motivo = corpo.motivo.strip()
    if not motivo:
        raise HTTPException(422, "registrar uma suspeita exige o motivo")
    alerta = _alerta_ou_404(conn, alerta_id)

    resultado = contra_isca.cacar(conn, alerta_id)
    do_cliente = {l["operacao_id"]: l for l in resultado["ligacoes"]
                  if l["cliente_id"] == corpo.cliente_id}
    pedidas = list(dict.fromkeys(corpo.operacoes))   # sem repetir, na ordem pedida
    faltando = [o for o in pedidas if o not in do_cliente]
    if faltando:
        raise HTTPException(
            409,
            f"{', '.join(faltando)} não {'é ligação' if len(faltando) == 1 else 'são ligações'} "
            f"atual(is) de {corpo.cliente_id} com o caso {alerta['cliente_id']} - "
            "recarregue a caça e registre de novo",
        )

    ja = conn.execute(
        "SELECT analista_id, registrado_em FROM suspeitas WHERE alerta_origem_id = ? "
        "AND cliente_id = ? AND operacoes_json = ?",
        (alerta_id, corpo.cliente_id, json.dumps(sorted(pedidas))),
    ).fetchone()
    if ja is not None:
        raise HTTPException(
            409, f"essa suspeita já foi registrada por {ja['analista_id']} em {ja['registrado_em']}"
        )

    retrato = [do_cliente[o] for o in pedidas]
    registrado_em = db.agora_utc()
    with conn:
        cur = conn.execute(
            "INSERT INTO suspeitas (alerta_origem_id, cliente_id, operacoes_json, ligacoes_json, "
            "versao_caca, analista_id, motivo, registrado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (alerta_id, corpo.cliente_id, json.dumps(sorted(pedidas)),
             json.dumps(retrato, ensure_ascii=False), resultado["versao_caca"], analista,
             motivo, registrado_em),
        )
    return _suspeitas(conn, "s.id = ?", cur.lastrowid)[0]


# ============================================================================
# Fase 4.2 / 4.3 - metricas sobre as decisoes
# ============================================================================


@app.get("/metricas", response_model=Metricas)
def ver_metricas(
    execucao_id: int | None = None,
    linha_de_base_min: float | None = Query(None, gt=0, le=24 * 60),
    conn: sqlite3.Connection = Depends(conexao),
):
    """Agente x analista, aderencia x decisao, e o tempo de analise medido.

    `linha_de_base_min` e o tempo de analise por caso SEM a ferramenta. O store
    nao o tem e nao o inventa: so com ele informado a resposta traz economia, e
    traz junto a procedencia ("informada, nao medida")."""
    # sem execucao_id: todas as decisoes do store, sobre os casos vigentes
    alvo = None if execucao_id is None else _execucao(conn, execucao_id)
    if alvo is None:
        _execucao(conn, None)  # 503 se nao ha execucao nenhuma
    return metricas.calcular(conn, alvo, linha_de_base_min)


# ============================================================================
# Fase 3 - a tela
# ============================================================================


@app.get("/", include_in_schema=False)
def raiz():
    return RedirectResponse("/app/")


ARQUIVOS_VERSIONADOS = ("app.js", "logica.js", "estilo.css")


def _versao_da_tela() -> str:
    """Hash do conteudo dos arquivos da tela. Calculado a cada pedido do
    index.html (sao ~40 KB): com --reload, ou depois de atualizar o codigo, a
    versao ja muda sem reiniciar nada."""
    h = hashlib.sha256()
    for nome in ARQUIVOS_VERSIONADOS:
        h.update((WEB_DIR / nome).read_bytes())
    return h.hexdigest()[:12]


@app.get("/app/", include_in_schema=False)
def tela(request: Request):
    """O index.html com os enderecos versionados (ver o comentario nele).

    O no-cache sozinho nao bastou: copias guardadas ANTES de o cabecalho existir
    continuavam sendo reusadas sem o navegador perguntar nada - o log do
    servidor mostrava so o GET do index, nunca o do app.js, mesmo apos recarga.
    Com o endereco mudando junto com o conteudo, o navegador nao tem copia para
    reusar."""
    versao = _versao_da_tela()
    etag = f'"{versao}-{hashlib.sha256((WEB_DIR / "index.html").read_bytes()).hexdigest()[:12]}"'
    cabecalhos = {"Cache-Control": "no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cabecalhos)
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("{{versao}}", versao)
    return HTMLResponse(html, headers=cabecalhos)


class _TelaSemCacheVelho(StaticFiles):
    """Os arquivos da tela com `Cache-Control: no-cache`.

    Sem isso o navegador reusa o app.js antigo SEM perguntar ao servidor
    (cache heuristico, pelo Last-Modified): visto de verdade na Fase 4 - depois
    de atualizar o codigo, o index.html novo mostrava o botao "Metricas", mas o
    app.js em cache nao conhecia a rota e o clique nao fazia nada. no-cache NAO
    desliga o cache: obriga a revalidar, e sem mudanca a resposta e um 304."""

    async def get_response(self, path, scope):
        resposta = await super().get_response(path, scope)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


# Montado por ULTIMO: um mount captura o prefixo inteiro, e registrado antes das
# rotas poderia sombrear alguma. html=True serve o index.html em /app/.
app.mount("/app", _TelaSemCacheVelho(directory=WEB_DIR, html=True), name="tela")
