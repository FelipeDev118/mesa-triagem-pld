# Desafio Técnico — Estágio em Engenharia de IA

[![CI](https://github.com/FelipeDev118/desafio-itau-ai-engineer/actions/workflows/ci.yml/badge.svg)](https://github.com/FelipeDev118/desafio-itau-ai-engineer/actions/workflows/ci.yml)

Triagem de operações financeiras para Prevenção à Lavagem de Dinheiro (PLD/AML), combinando
**regras determinísticas em pandas** (o que é cálculo) com um **LLM** (o que é interpretação e
redação de parecer).

O princípio que guia todo o projeto: soma, mediana, contagem e comparação com limite são feitas
em pandas e **entregues prontas ao modelo como fato**; o LLM nunca calcula nem decide se um
número ultrapassou um limite — ele interpreta o padrão e redige o parecer.

## Como rodar

### Com Docker (recomendado)

```bash
cp .env.example .env                  # preencha GROQ_API_KEY
docker compose run --rm verificar     # smoke test: não precisa de chave nem chama LLM
docker compose up notebook            # Nível 1 em http://localhost:8888
docker compose run --rm nivel2        # regras em escala + lote + confronto
docker compose run --rm nivel3        # agente via MCP + comparação de transportes
```

O `verificar` reexecuta a camada determinística e compara com os números desta entrega —
é a prova de que o pipeline de regras reproduz em qualquer máquina. Detalhes e o que ele
deliberadamente **não** garante em [`docs/REPRODUTIBILIDADE.md`](docs/REPRODUTIBILIDADE.md).

### Sem Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # preencha GROQ_API_KEY com sua chave
python verificar_ambiente.py
```

Nível 1 (notebook, já commitado com as saídas executadas):

```bash
jupyter notebook nivel_1/nivel_1.ipynb
```

Nível 2 (a partir da pasta `nivel_2/`):

```bash
cd nivel_2
python dados.py        # regras em escala + top 10 clientes sinalizados
python lote.py         # roda o agente sobre os 30 clientes da base -> outputs/
python confronto.py    # confronta regra vs. agente -> outputs/
```

Nível 3 — MCP (a partir da raiz do projeto; o cliente sobe o servidor sozinho):

```bash
python nivel_3/agente_mcp.py --lote        # agente consumindo as tools por MCP
python nivel_3/comparar_transportes.py     # valida MCP vs. import direto
```

## Mesa de Triagem — a ferramenta do analista (evolução pós-entrega)

Depois da entrega, o pipeline em lote virou um sistema de uso diário: o analista abre um caso,
vê cada número do parecer ligado à operação de onde veio, e registra a decisão. **O sistema
tria, o humano decide** — nada é concluído sem decisão registrada, e nada é reportado sozinho.
Todo o desenvolvimento, passo a passo e com a evidência de cada aceite, está em
[`.claude/features/ROADMAP.txt`](.claude/features/ROADMAP.txt).

```bash
python -m mesa.ingestao && python -m mesa.regras_run   # store: base + regras + fila
python -m mesa.importar_cache                          # histórico de pareceres (sem LLM)
python -m mesa.triagem                                 # worker: parecer para cada alerta
uvicorn mesa.api:app                                   # tela em http://127.0.0.1:8000
python -m mesa.ciclo --entrada dados/entrada           # base nova: só o delta é reprocessado
python -m mesa.db                                      # migra um store de versão anterior
python -m mesa.cenario_isca --semente 7                # cenário de isca plantado, com gabarito
python -m mesa.cenario_isca --medir 101-120            # acerto da contra-isca contra o gabarito
```

Com Docker: `docker compose run --rm mesa`, `docker compose run --rm mesa-triagem`,
`docker compose up api` e, para rodar sozinho a cada hora, `docker compose up -d mesa-ciclo`.

**Demonstração pública (Render):** `Dockerfile.demo` monta o store no build, a partir da base
sintética e do cache de pareceres (sem chave, sem LLM), e sobe a API. `render.yaml` é o blueprint
(Render → New → Blueprint → este repositório). A tela mostra a faixa "demonstração · dados
sintéticos"; não há login, e o disco é efêmero: cada reinício volta ao estado inicial. Para dado
real, a Fase 7 do roadmap (LGPD + identidade) vem antes.
Localmente: `docker build -f Dockerfile.demo -t mesa-demo . && docker run --rm -p 127.0.0.1:8000:8000 mesa-demo`.

| Caminho | O que é |
|---|---|
| `mesa/esquema.sql`, `mesa/db.py` | Store SQLite (esquema v8), migração por versão. Parecer, decisão, trilha e suspeita são append-only por trigger. |
| `mesa/ingestao.py` | Arquivo → store. Operação corrigida vira versão nova; a anterior fica no histórico. |
| `mesa/regras_run.py` | Regras sobre o store; execução incremental (só clientes que mudaram). |
| `mesa/triagem.py` | Worker: reaproveita parecer quando a entrada não mudou (zero chamada de LLM). |
| `mesa/api.py` | API FastAPI — nenhum cálculo de regra; decisão e trilha numa transação. |
| `mesa/metricas.py` | Agente × analista, aderência × decisão, tempo de análise medido pela trilha. |
| `mesa/ciclo.py` | Ingere o que chegou, reavalia o delta, tria — agendável (cron, `--a-cada`). |
| `mesa/contra_isca.py` | "Caçar o que passou": operações de **outros** clientes ligadas a um caso chamativo — fracionamento distribuído na janela de dias, ou volume que entrou enquanto o caso estava em análise. Cada parte do escore com a sua procedência; o analista assina a suspeita. |
| `mesa/cenario_isca.py` | Cenário plantado sobre a base real (isca + os dois padrões + ruído), com gabarito — a prova de que a caça acha o que foi plantado, e quanto lixo traz. |
| `mesa/web/` | A tela, em JavaScript puro, servida pela própria API. |

## Estrutura

A estrutura obrigatória do enunciado foi seguida à risca. Os arquivos marcados com ➕ são
**adições** a ela — módulos extraídos para não inchar os arquivos exigidos, e os bônus (Docker,
observabilidade de custo). Nenhum arquivo obrigatório foi renomeado, movido ou substituído.

| Caminho | O que é |
|---|---|
| `nivel_1/nivel_1.ipynb` | Tratamento de dados, regras determinísticas, validação e parecer via LLM (com as saídas executadas). |
| `nivel_2/tools.py` | As três ferramentas que o agente pode consultar. |
| `nivel_2/agente.py` | Agente com function calling nativo — o modelo decide quais tools chamar. |
| `nivel_2/confronto.py` | Confronto entre `nivel_risco` do agente e as flags determinísticas. |
| ➕ `nivel_2/dados.py` | Carga, limpeza e regras reaproveitadas do Nível 1 sobre a base maior. |
| ➕ `nivel_2/lote.py` | Execução em lote sobre os 30 clientes da base (`todos=False` restringe aos mais sinalizados). |
| ➕ `nivel_2/observabilidade.py` | Custo e latência por chamada de API (bônus). |
| ➕ `nivel_2/cache_parecer.py` | Cache por hash da entrada — reprodutibilidade entre execuções. |
| ➕ `nivel_2/verificacao_aderencia.py` | Confere se o parecer cita números que existem na base. |
| `nivel_3/mcp_server.py` | Servidor MCP (stdio) que republica as ferramentas do Nível 2. |
| `nivel_3/agente_mcp.py` | Agente que consome as ferramentas por MCP, não por import direto. |
| `nivel_3/comparar_transportes.py` | Valida que a troca de transporte preserva o comportamento. |
| `outputs/` | Resultados salvos de todas as execuções. |
| `docs/DECISOES.md` | Trade-offs, limitações e o que faria com mais tempo. |
| `docs/USO_DE_IA.md` | Como usei IA e onde ela me levou ao caminho errado. |
| ➕ `docs/ARQUITETURA.md` | Arquitetura do Nível 3 (MCP) e como conectar. |
| ➕ `docs/REPRODUTIBILIDADE.md` | O que é reproduzível nesta solução, e o que não é. |
| ➕ `Dockerfile`, `docker-compose.yml` | Ambiente containerizado (bônus). |
| ➕ `verificar_ambiente.py` | Smoke test: reexecuta a camada determinística e compara com a entrega. |

## O que foi concluído

- **Nível 1 — completo.** Limpeza (3 problemas de qualidade encontrados e tratados), duas regras
  determinísticas, validação explícita da Regra 1 (caso positivo vs. caso parecido que não se
  enquadra), parecer estruturado validado com Pydantic e tratamento de resposta malformada,
  duas versões de prompt comparadas com métricas de tokens e latência.
- **Nível 2 — completo.** Regras reaproveitadas em escala sem reescrita, três ferramentas,
  agente com function calling nativo (decide quais ferramentas usar, não chama todas sempre),
  lote sobre os **30 clientes da base** com registro de custo/latência por chamada, e confronto
  regra vs. modelo com análise das divergências.
- **Nível 3 — completo (Trilha B).** As ferramentas do Nível 2 são expostas por um servidor MCP
  local via stdio e consumidas pelo protocolo, com descoberta em runtime — o agente não tem mais
  a lista de ferramentas hardcoded. Validado comparando as duas vias: payload das ferramentas
  idêntico em 6/6 casos. Arquitetura e instruções de conexão em
  [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md).

## Alguns achados da execução

- O **prompt v1 do Nível 1 é instável**, não simplesmente errado: executado duas vezes com o
  mesmo texto e `temperature=0.2`, numa delas alucinou um limite de reporte de "≈ R$ 10.000" que
  não existe no enunciado (o real é R$ 20.000,00) e na outra acertou. Por ser subespecificado,
  ele deixa o modelo preencher a lacuna com conhecimento genérico. O v2 não tem essa lacuna
  porque recebe o número já calculado. A execução original está no commit `9a59dd8`.
- O modelo tenta, ocasionalmente, chamar uma **ferramenta fictícia chamada `JSON`** para devolver
  a resposta final, o que a API rejeita; o parecer válido vem dentro do corpo do erro e é
  recuperado de lá (`nivel_2/agente.py`).
- O agente **decide** quais ferramentas usar. A auditoria das saídas revelou dois **defeitos de
  desenho do prompt**, ambos corrigidos:
  - Clientes com flag de fracionamento não consultavam o recorte diário, porque o prompt
    informava *que* a flag estava ativa sem informar *em qual data* (`nivel_2/dados.py`,
    `datas_fracionamento` + `montar_flags`).
  - Clientes **sem** flag de fracionamento (só valor atípico) faziam o agente "pescar"
    `operacoes_do_dia` com datas chutadas, sem grounding — consumindo os 4 turnos disponíveis sem
    sobrar um para a resposta final, esgotando `max_turnos` em 3 dos 10 clientes mais sinalizados.
    Corrigido deixando explícito no prompt que a ferramenta só deve ser chamada com uma data
    fornecida ou já observada — nunca inventada — e que não investigar por falta de pista é uma
    decisão válida. Resultado, confirmado depois nos 30 clientes da base: **30/30 respostas
    válidas** (era 7/10 antes da correção, sobre o top 10).
  Análise completa de ambos, incluindo efeitos colaterais descobertos em cada correção, em
  [`docs/DECISOES.md`](docs/DECISOES.md#o-agente-não-usava-operacoes_do_dia-nos-casos-de-fracionamento--corrigido).
- **Lote passou a cobrir os 30 clientes da base**, não só os 10 mais sinalizados — existe para
  medir o falso negativo que nenhuma métrica anterior media: um cliente sem flag determinística
  ainda seria marcado como risco pelo agente? Nesta base, não: os 13 clientes sem nenhuma
  sinalização concordaram trivialmente com `baixo` risco. `nivel_2/dados.py` ganhou
  `todos_os_clientes()`; `lote.py` e `confronto.py` usam por padrão (`todos=False` volta ao
  comportamento antigo). Detalhes em
  [`docs/DECISOES.md`](docs/DECISOES.md#cobrir-os-30-clientes--corrigido).
- A **concordância entre regra e agente variou entre execuções** (50%, depois 30%, com código
  equivalente e os mesmos dados) — essa instabilidade em si é um achado central. Sobre os 30
  clientes: **77% de concordância no agregado, mas 59% (10/17) só entre os sinalizados** — os
  outros 13 concordam trivialmente em `baixo`, sem sinal nenhum para discordar; misturar os dois
  infla o número. Dos 7 divergentes entre os sinalizados, **6 vão na mesma direção** (regra `alto`
  → agente `médio`) — diferença sistemática de limiar, não ruído — mas **1 vai na direção oposta**
  (`CLI-021`), o que revisa a afirmação anterior de "sempre a mesma direção": com mais clientes,
  "sistemática" virou "predominantemente sistemática, com uma exceção". A verificação de aderência
  (ver abaixo) mostra que, na maioria dos casos (28 de 30), o raciocínio parte de premissas
  corretas, então a divergência não é (só) leitura errada dos dados. Análise em
  [`docs/DECISOES.md`](docs/DECISOES.md#critério-do-confronto-intensidade-não-coincidência-de-regras).
- **Cache por hash de entrada** (`nivel_2/cache_parecer.py`) resolve essa instabilidade *entre
  reexecuções do pipeline*: parecer já gerado é reaproveitado em vez de recalculado. Provado, não
  só afirmado — rodar `lote.py` a 3ª vez levou 1,3s com **0 chamadas de API** (a 1ª levou 3min47),
  e `confronto.py` passou a dar o mesmo número em execuções seguidas. O hash inclui a versão do
  prompt, então mudar o `SYSTEM_PROMPT` (como nas duas correções acima) invalida o cache de
  propósito, em vez de reaproveitar parecer gerado com instrução antiga.
- A métrica de concordância também estava **enganosa por omissão**: misturava "o agente
  discordou" com "o agente não respondeu" (turnos excedidos). `confronto.py` separa
  `respostas_validas` de `taxa_concordancia_entre_validas` — a taxa de resposta válida virou um
  número auditável por si só, não escondido dentro da concordância.
- **O achado mais forte de todos**: uma verificação de aderência sem LLM
  (`nivel_2/verificacao_aderencia.py`) confere se os valores citados na justificativa existem de
  fato na base do cliente. Primeira leitura pós-correção do `max_turnos` (sobre 10 clientes): só
  3/10 pareceres fundamentados, pior que os 4/7 (~57%) de antes — e essa leitura **estava
  errada**. Reauditando cada caso contra os dados reais, achei **bugs no próprio verificador**:
  mediana confundida com uma operação real quando o cliente tem número ímpar de operações; regex
  que truncava números em formato americano (`R$71,297.68` virava `71.29`) e abreviações como
  `R$14.3k`; soma por canal ausente das referências válidas, embora `perfil_canal()` seja uma das
  3 ferramentas do agente; tolerância rígida demais para valores abreviados arredondados; e "sem
  valor citado" tratado como falha mesmo quando o cliente não tem nenhuma flag e não há nada para
  citar. Corrigidos, um a um, contra evidência real de cada caso, e confirmados sobre os 30
  clientes da base: **28/30 pareceres fundamentados**. Dos 2 que restam, só 1 é erro real do
  agente: `CLI-028` chamou de "único evento de valor atípico" uma operação de R$ 6.913,84, quando
  as duas operações que de fato têm a flag ativa para esse cliente são de R$ 27.715,48 e
  R$ 24.875,39 — mais que o dobro; o outro (`CLI-001`) tem flag ativa e não cita nenhum número. A
  lição maior que o número: quase virou "o fix do max_turnos piorou a aderência" com base num
  verificador que, ele mesmo, não tinha sido verificado. Análise completa e testes de regressão em
  [`docs/DECISOES.md`](docs/DECISOES.md#o-verificador-de-aderência-tinha-bugs-próprios--corrigido)
  e [`tests/test_verificacao_aderencia.py`](tests/test_verificacao_aderencia.py). Resultado em
  [`outputs/aderencia_pareceres.csv`](outputs/aderencia_pareceres.csv).
