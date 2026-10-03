-- Esquema da Mesa de Triagem PLD.
--
-- Um arquivo so, de proposito: e o DDL inteiro num lugar auditavel, e e o que
-- torna a troca para Postgres uma edicao localizada (todo o SQL abaixo e
-- portavel, exceto os triggers da Fase 1 - ver .claude/features/ROADMAP.txt).
--
-- Convencoes:
--   - timestamps em TEXT ISO-8601 UTC ('2026-09-12T14:22:03Z')
--   - datas de operacao em TEXT 'YYYY-MM-DD' (SQLite nao tem tipo data; nesse
--     formato a ordenacao lexicografica coincide com a cronologica)
--   - booleanos em INTEGER 0/1
--   - *_json guarda JSON serializado; e detalhe de evidencia, nao campo de busca

-- ===========================================================================
-- FASE 0 - FATOS E ALERTAS
-- ===========================================================================

-- Cada arquivo/carga que entrou no sistema. A taxa de cambio e propriedade do
-- LOTE, nao uma constante global: dois lotes podem ter taxas diferentes, e um
-- alerta de ontem precisa continuar explicavel pela taxa que valia ontem.
CREATE TABLE IF NOT EXISTS lotes_ingestao (
    id                   INTEGER PRIMARY KEY,
    origem               TEXT    NOT NULL,
    sha256_arquivo       TEXT    NOT NULL,
    taxa_cambio_usd_brl  REAL    NOT NULL,
    operacoes_brutas     INTEGER NOT NULL,
    operacoes_inseridas  INTEGER NOT NULL,
    duplicatas_ignoradas INTEGER NOT NULL,
    -- Estatisticas do ARQUIVO, antes da limpeza. Ficam aqui porque nao existem
    -- em lugar nenhum depois: a tabela operacoes so guarda o que sobreviveu a
    -- deduplicacao. Sem elas, "o arquivo tinha 7 datas nulas" vira uma afirmacao
    -- sem fonte no store (uma das 5 duplicatas removidas tinha data nula, entao
    -- operacoes tem 6 - os dois numeros estao certos, sobre populacoes
    -- diferentes). E o que permite ao passo 0.8 reproduzir os 8 numeros da
    -- entrega lendo so o banco.
    datas_nulas_brutas   INTEGER NOT NULL,
    operacoes_usd_brutas INTEGER NOT NULL,
    ingerido_em          TEXT    NOT NULL
);

-- Fato imutavel. A deduplicacao por id deixa de ser uma linha de pandas e vira
-- restricao do banco: nao da para reintroduzir duplicata por engano.
CREATE TABLE IF NOT EXISTS operacoes (
    id          TEXT    PRIMARY KEY,
    lote_id     INTEGER NOT NULL REFERENCES lotes_ingestao(id),
    cliente_id  TEXT    NOT NULL,
    data        TEXT,                 -- NULL quando ausente ou nao parseavel
    data_valida INTEGER NOT NULL,     -- 0/1, espelha df["data_valida"]
    valor       REAL    NOT NULL,     -- valor original, na moeda original
    moeda       TEXT    NOT NULL,
    -- Derivado na ingestao com a taxa do lote e materializado ao lado do bruto.
    -- Redundancia deliberada: valor+moeda preservam o fato como chegou,
    -- valor_brl congela QUAL taxa foi aplicada QUANDO. Recalcular na leitura
    -- reescreveria a historia quando a taxa mudasse.
    valor_brl   REAL    NOT NULL,
    canal       TEXT    NOT NULL,
    tipo        TEXT    NOT NULL,
    contraparte TEXT    NOT NULL,
    observacao  TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_op_cliente      ON operacoes(cliente_id);
-- E este indice que corrige a limitacao documentada em docs/DECISOES.md:
-- operacoes_do_dia() deixa de varrer a base inteira para responder sobre um dia.
CREATE INDEX IF NOT EXISTS idx_op_cliente_data ON operacoes(cliente_id, data);

-- Uma execucao das regras sobre um lote. Os PARAMETROS ficam gravados: sem
-- isso, um alerta de seis meses atras e inexplicavel se o limiar mudou desde
-- entao ("por que R$ 50.000?" nao pode depender de arqueologia no git).
CREATE TABLE IF NOT EXISTS execucoes_regras (
    id                     INTEGER PRIMARY KEY,
    lote_id                INTEGER NOT NULL REFERENCES lotes_ingestao(id),
    versao_regras          TEXT    NOT NULL,
    parametros_json        TEXT    NOT NULL,
    executado_em           TEXT    NOT NULL,
    operacoes_avaliadas    INTEGER NOT NULL,
    clientes_fracionamento INTEGER NOT NULL,
    operacoes_atipicas     INTEGER NOT NULL
);

-- Cada disparo individual de regra: e o que liga o alerta a evidencia exata.
-- Torna datas_fracionamento() um SELECT em vez de um recalculo de
-- flag_fracionamento() inteira so para descobrir uma data.
CREATE TABLE IF NOT EXISTS sinalizacoes (
    id           INTEGER PRIMARY KEY,
    execucao_id  INTEGER NOT NULL REFERENCES execucoes_regras(id),
    cliente_id   TEXT    NOT NULL,
    regra        TEXT    NOT NULL,   -- 'fracionamento' | 'valor_atipico'
    operacao_id  TEXT             REFERENCES operacoes(id),  -- NULL no fracionamento
    data         TEXT,               -- preenchida no fracionamento (o dia que disparou)
    detalhe_json TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sin_cliente ON sinalizacoes(execucao_id, cliente_id);
CREATE INDEX IF NOT EXISTS idx_sin_regra   ON sinalizacoes(execucao_id, regra);

-- A fila do analista.
--
-- origem='controle' existe para resolver uma tensao real: chamar de "alerta" um
-- cliente com zero sinalizacoes e contraditorio, mas os 13 clientes sem flag sao
-- justamente o que mede o falso negativo (ver DECISOES.md, "Cobrir os 30
-- clientes"). A fila do analista filtra origem='regra'; a metrica de falso
-- negativo continua rodando sobre os de controle.
CREATE TABLE IF NOT EXISTS alertas (
    id                         INTEGER PRIMARY KEY,
    execucao_id                INTEGER NOT NULL REFERENCES execucoes_regras(id),
    cliente_id                 TEXT    NOT NULL,
    origem                     TEXT    NOT NULL CHECK (origem IN ('regra', 'controle')),
    sinalizacoes_fracionamento INTEGER NOT NULL,
    sinalizacoes_valor_atipico INTEGER NOT NULL,
    total_sinalizacoes         INTEGER NOT NULL,
    volume_total_brl           REAL    NOT NULL,
    qtd_operacoes              INTEGER NOT NULL,
    nivel_risco_regra          TEXT    NOT NULL,
    estado                     TEXT    NOT NULL DEFAULT 'novo'
        CHECK (estado IN ('novo', 'triado', 'em_analise', 'concluido')),
    analista_id                TEXT,
    criado_em                  TEXT    NOT NULL,
    UNIQUE (execucao_id, cliente_id)
);

-- Mesma ordenacao de ranking_clientes_sinalizados(): sinalizacoes desc, volume desc.
CREATE INDEX IF NOT EXISTS idx_alertas_fila
    ON alertas(estado, total_sinalizacoes DESC, volume_total_brl DESC);

-- ===========================================================================
-- FASE 1 - PARECER COMO REGISTRO, NAO COMO CACHE
-- ===========================================================================

-- Log APPEND-ONLY de pareceres. A diferenca em relacao ao cache JSON que isto
-- substitui nao e o formato de armazenamento: e o modelo mental. O cache era um
-- mapa hash -> parecer, sobrescrivivel; aqui o mesmo hash pode ter N linhas ao
-- longo do tempo, e "o parecer atual" e sempre a mais recente. E o que responde
-- "por que este cliente foi alto risco em 15/03" mesmo depois de o parecer ter
-- mudado - a pergunta que um pipeline recalculavel nao consegue responder.
--
-- alerta_id e NULLABLE de proposito: rodar o agente fora da fila (o
-- `python nivel_2/agente.py` da entrega, ou um teste) produz parecer legitimo
-- sem alerta associado. Exigir o vinculo faria a entrega parar de rodar.
CREATE TABLE IF NOT EXISTS pareceres (
    id                 INTEGER PRIMARY KEY,
    alerta_id          INTEGER          REFERENCES alertas(id),
    cliente_id         TEXT    NOT NULL,
    hash_entrada       TEXT    NOT NULL,
    -- quando preenchido, esta linha e uma copia reaproveitada de outra: mesmo
    -- conteudo, alerta novo, ZERO chamada de LLM. Ver mesa/pareceres.py.
    reaproveitado_de   INTEGER          REFERENCES pareceres(id),
    nivel_risco        TEXT,            -- NULL quando erro_parsing
    tipologia_suspeita TEXT,
    red_flags_json     TEXT,
    justificativa      TEXT,
    erro_parsing       TEXT,
    texto_bruto        TEXT,
    modelo             TEXT    NOT NULL,
    versao_prompt      TEXT    NOT NULL,
    transporte         TEXT    NOT NULL DEFAULT 'import_direto',
    -- 'agente'  : gerado nesta instalacao, criado_em OBSERVADO
    -- 'importado_cache': trazido de outputs/cache_pareceres.json, criado_em
    --            INFERIDO do mtime do arquivo. Um timestamp inferido nao pode se
    --            passar por observado numa trilha de auditoria - quem consultar
    --            "o que sabiamos em 15/03" precisa saber a diferenca.
    origem_registro    TEXT    NOT NULL DEFAULT 'agente'
        CHECK (origem_registro IN ('agente', 'importado_cache')),
    tokens_total       INTEGER,
    latencia_s         REAL,
    criado_em          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_par_hash   ON pareceres(hash_entrada, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_par_alerta ON pareceres(alerta_id, criado_em DESC);

-- O append-only vira invariante do SCHEMA, nao convencao de codigo. Uma regra
-- que depende de todo mundo lembrar dela nao e uma regra - e uma esperanca.
CREATE TRIGGER IF NOT EXISTS pareceres_sem_update BEFORE UPDATE ON pareceres
BEGIN
    SELECT RAISE(ABORT, 'pareceres e append-only: insira uma nova versao');
END;

CREATE TRIGGER IF NOT EXISTS pareceres_sem_delete BEFORE DELETE ON pareceres
BEGIN
    SELECT RAISE(ABORT, 'pareceres e append-only: nao se apaga decisao de risco');
END;

-- O que o agente VIU quando decidiu. Hoje o agente guarda so {tool, args} e
-- descarta o retorno - o que torna um caso reaberto seis meses depois
-- irreproduzivel: a base ja mudou. Guardando o payload, a tela de caso pode
-- mostrar a evidencia como ela era, nao como esta agora.
CREATE TABLE IF NOT EXISTS evidencias (
    id           INTEGER PRIMARY KEY,
    parecer_id   INTEGER NOT NULL REFERENCES pareceres(id),
    ordem        INTEGER NOT NULL,
    tool         TEXT    NOT NULL,
    args_json    TEXT    NOT NULL,
    payload_json TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evid_parecer ON evidencias(parecer_id, ordem);

-- Resultado do grounding check (nivel_2/verificacao_aderencia.py), gravado junto
-- do parecer que ele auditou. Nao bloqueia nada: a tela do analista MARCA o
-- valor nao confirmado, nao esconde o parecer.
CREATE TABLE IF NOT EXISTS aderencia (
    parecer_id                   INTEGER PRIMARY KEY REFERENCES pareceres(id),
    fundamentado                 INTEGER NOT NULL,
    motivo                       TEXT    NOT NULL,
    valores_confirmados_json     TEXT    NOT NULL,
    valores_nao_encontrados_json TEXT    NOT NULL,
    atipicos_incorretos_json     TEXT    NOT NULL,
    -- ONDE cada valor citado esta na justificativa e como foi classificado:
    -- [{inicio, fim, valor, classe, fonte}]. Gravado na MESMA execucao do
    -- verificador que produziu a classificacao, para as duas coisas nunca
    -- divergirem - e para a tela (Fase 3) nao precisar de um segundo parser.
    marcas_json                  TEXT    NOT NULL DEFAULT '[]',
    verificado_em                TEXT    NOT NULL
);

-- Uma linha por REQUISICAO a API (nao por cliente): e a granularidade que
-- nivel_2/observabilidade.py ja produz, e a unica que responde "qual turno custa
-- caro" - decidir ferramenta ou redigir o parecer.
CREATE TABLE IF NOT EXISTS chamadas_llm (
    id                    INTEGER PRIMARY KEY,
    parecer_id            INTEGER REFERENCES pareceres(id),
    cliente_id            TEXT    NOT NULL,
    turno                 INTEGER NOT NULL,
    tipo_turno            TEXT    NOT NULL,
    modelo                TEXT    NOT NULL,
    tokens_entrada        INTEGER NOT NULL,
    tokens_saida          INTEGER NOT NULL,
    tokens_total          INTEGER NOT NULL,
    latencia_s            REAL    NOT NULL,
    custo_usd             REAL    NOT NULL,
    transporte            TEXT    NOT NULL,
    tentativas_rate_limit INTEGER NOT NULL,
    registrado_em         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chamadas_parecer ON chamadas_llm(parecer_id);

-- ===========================================================================
-- FASE 4 - O HUMANO DECIDE, E A DECISAO VIRA REGISTRO
-- ===========================================================================

-- Trilha de TODA mudanca de estado de um caso: quem, de onde, para onde,
-- quando. alertas.estado/analista_id dizem so o AGORA - quem pegou o caso ontem
-- e devolveu some dali. Para compliance, o caminho importa tanto quanto o fim.
--
-- ator_tipo separa pessoa de processo: 'sistema' e o worker de triagem
-- (ator 'sistema:triagem'), 'analista' e quem veio no header X-Analista.
CREATE TABLE IF NOT EXISTS transicoes (
    id              INTEGER PRIMARY KEY,
    alerta_id       INTEGER NOT NULL REFERENCES alertas(id),
    estado_anterior TEXT    NOT NULL
        CHECK (estado_anterior IN ('novo', 'triado', 'em_analise', 'concluido')),
    estado_novo     TEXT    NOT NULL
        CHECK (estado_novo IN ('novo', 'triado', 'em_analise', 'concluido')),
    ator            TEXT    NOT NULL,
    ator_tipo       TEXT    NOT NULL CHECK (ator_tipo IN ('analista', 'sistema')),
    registrado_em   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trans_alerta ON transicoes(alerta_id, id);

CREATE TRIGGER IF NOT EXISTS transicoes_sem_update BEFORE UPDATE ON transicoes
BEGIN
    SELECT RAISE(ABORT, 'transicoes e append-only: a trilha nao se reescreve');
END;

CREATE TRIGGER IF NOT EXISTS transicoes_sem_delete BEFORE DELETE ON transicoes
BEGIN
    SELECT RAISE(ABORT, 'transicoes e append-only: a trilha nao se apaga');
END;

-- A decisao do analista. Uma por caso (UNIQUE): `concluido` e terminal nesta
-- fase - reabrir caso decidido e papel de supervisor, que exige autenticacao
-- de verdade. O UNIQUE faz o BANCO garantir isso, alem do compare-and-set.
--
-- parecer_id e o parecer que o analista VIU ao decidir (a API recusa decisao
-- sobre parecer diferente do atual). NULL quando o caso foi decidido sem
-- parecer - um caso pego antes da triagem, que o worker nao toca.
--
-- nivel_risco_agente e COPIADO do parecer visto, normalizado, em vez de lido
-- por join na hora da metrica: a metrica agente-vs-humano compara com o que o
-- analista tinha na frente, e isso nao pode depender de consulta nenhuma.
CREATE TABLE IF NOT EXISTS decisoes (
    id                   INTEGER PRIMARY KEY,
    alerta_id            INTEGER NOT NULL UNIQUE REFERENCES alertas(id),
    parecer_id           INTEGER          REFERENCES pareceres(id),
    analista_id          TEXT    NOT NULL,
    decisao              TEXT    NOT NULL CHECK (decisao IN ('concordo', 'discordo', 'escalar')),
    nivel_risco_analista TEXT             CHECK (nivel_risco_analista IN ('baixo', 'médio', 'alto')),
    nivel_risco_agente   TEXT             CHECK (nivel_risco_agente IN ('baixo', 'médio', 'alto')),
    motivo               TEXT,
    decidido_em          TEXT    NOT NULL,
    -- as regras de cada decisao tambem no schema, nao so na API: concordar
    -- exige um nivel do agente com que concordar; discordar exige o nivel
    -- proposto e o porque; escalar exige o porque.
    -- CUIDADO ao mexer: um CHECK cuja expressao da NULL PASSA. `length(trim(
    -- NULL)) > 0` e NULL, e deixava "escalar sem motivo" entrar - pego pelo
    -- teste. Por isso o COALESCE e o `IS` (que compara NULL como valor) no
    -- lugar de `=`.
    CHECK (decisao != 'concordo' OR (nivel_risco_agente IS NOT NULL
                                     AND nivel_risco_analista IS nivel_risco_agente)),
    CHECK (decisao != 'discordo' OR (nivel_risco_analista IS NOT NULL
                                     AND COALESCE(length(trim(motivo)), 0) > 0)),
    CHECK (decisao != 'escalar' OR COALESCE(length(trim(motivo)), 0) > 0)
);

CREATE TRIGGER IF NOT EXISTS decisoes_sem_update BEFORE UPDATE ON decisoes
BEGIN
    SELECT RAISE(ABORT, 'decisoes e append-only: decisao registrada nao se altera');
END;

CREATE TRIGGER IF NOT EXISTS decisoes_sem_delete BEFORE DELETE ON decisoes
BEGIN
    SELECT RAISE(ABORT, 'decisoes e append-only: decisao registrada nao se apaga');
END;
