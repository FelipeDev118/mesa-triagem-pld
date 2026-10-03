// Tela da Mesa de Triagem - DOM e rede. A logica pura (e testada) fica em
// logica.js; aqui so se busca na API e se desenha.
//
// SEGURANCA: a justificativa do parecer e texto gerado por um LLM. Nada que
// venha da API e inserido como HTML - tudo entra como no de texto, pela funcao
// h() abaixo. Um innerHTML com o texto do modelo transformaria uma saida
// malformada (ou maliciosa) em codigo rodando no navegador do analista.

import {
  agruparLigacoes, alvoDaFonte, camposDaDecisao, espessura, explicacaoDaMarca, formatarBRL, formatarData,
  formatarDuracao, formatarMomento, formatarPercentual, operacaoEhAlvo, podeDecidir, posicoesNoMapa,
  rotuloDaFonte, ROTULO_CACA, ROTULO_COMPONENTE, ROTULO_DECISAO, ROTULO_ESTADO, ROTULO_PADRAO,
  ROTULO_TRANSICAO, segmentar, situacaoDaFila, suspeitasDoCliente,
} from "./logica.js";

const $ = (seletor, raiz = document) => raiz.querySelector(seletor);

function h(tag, atributos, ...filhos) {
  const el = document.createElement(tag);
  for (const [chave, valor] of Object.entries(atributos ?? {})) {
    if (valor == null || valor === false) continue;
    if (chave === "class") el.className = valor;
    else if (chave === "dataset") Object.assign(el.dataset, valor);
    else if (chave.startsWith("on")) el.addEventListener(chave.slice(2), valor);
    else el.setAttribute(chave, valor === true ? "" : valor);
  }
  for (const filho of filhos.flat()) {
    if (filho == null || filho === false) continue;
    el.append(filho instanceof Node ? filho : document.createTextNode(String(filho)));
  }
  return el;
}

// ---------------------------------------------------------------- rede

class ErroApi extends Error {
  constructor(status, mensagem) {
    super(mensagem);
    this.status = status;
  }
}

async function api(caminho, opcoes = {}) {
  let resposta;
  try {
    resposta = await fetch(caminho, opcoes);
  } catch {
    throw new ErroApi(0, "Não foi possível falar com a API. Ela está rodando?");
  }
  let corpo = null;
  try { corpo = await resposta.json(); } catch { /* resposta sem corpo JSON */ }
  if (!resposta.ok) {
    // A API sempre devolve {detail: "..."} com a explicacao; 422 do FastAPI
    // devolve uma lista de erros de validacao.
    const d = corpo?.detail;
    const msg = typeof d === "string" ? d
      : Array.isArray(d) ? d.map((x) => x.msg).join("; ")
      : `erro ${resposta.status}`;
    throw new ErroApi(resposta.status, msg);
  }
  return corpo;
}

// ---------------------------------------------------------------- estado

const estado = {
  itens: [],
  cursor: null,
  total: 0,
  alertaAberto: null,
  caso: null,
  evidencias: [],
  marcaSelecionada: null,
  // a caca do caso aberto (Fase 6) - so existe depois do clique no botao
  caca: null,
};

const CHAVE_ANALISTA = "mesa-triagem.analista";

// localStorage pode lancar (janela privada, dados do site bloqueados): o nome
// do analista e conveniencia, nunca motivo para a tela quebrar.
function lerAnalista() {
  try { return localStorage.getItem(CHAVE_ANALISTA) ?? ""; } catch { return ""; }
}
function gravarAnalista(nome) {
  try { localStorage.setItem(CHAVE_ANALISTA, nome); } catch { /* segue sem lembrar */ }
}

// ---------------------------------------------------------------- avisos

let timerAviso;
function avisar(mensagem, tipo = "ok") {
  const el = $("#aviso");
  el.textContent = mensagem;
  el.className = `aviso aviso-${tipo}`;
  el.hidden = false;
  clearTimeout(timerAviso);
  timerAviso = setTimeout(() => { el.hidden = true; }, 6000);
}

// ---------------------------------------------------------------- pecas

function classeNivel(nivel) {
  return nivel ? `nivel-${nivel.normalize("NFD").replace(/[\u0300-\u036f]/g, "")}` : "nivel-vazio";
}

function chipNivel(quem, nivel) {
  return h("span", { class: `chip ${classeNivel(nivel)}`, title: `nível de risco segundo ${quem}` },
    h("span", { class: "chip-quem" }, quem), nivel ?? "—");
}

const ROTULO_SITUACAO = {
  nao_triado: "não triado",
  concorda: "concorda",
  diverge: "diverge",
};

function chipSituacao(situacao) {
  return h("span", { class: `chip situacao situacao-${situacao}` }, ROTULO_SITUACAO[situacao]);
}

function marcaFundamentado(fundamentado) {
  if (fundamentado === true) return h("span", { class: "fund fund-ok" }, "✓ números conferem");
  if (fundamentado === false) return h("span", { class: "fund fund-erro" }, "✗ número sem procedência");
  return null;
}

function secao(titulo, ...conteudo) {
  return h("section", { class: "bloco" }, h("h3", {}, titulo), ...conteudo);
}

// ---------------------------------------------------------------- fila

async function carregarFila({ anexar = false } = {}) {
  const params = new URLSearchParams({ origem: $("#filtro-origem").value, limite: "50" });
  const filtroEstado = $("#filtro-estado").value;
  if (filtroEstado) params.set("estado", filtroEstado);
  // Paginacao pelo cursor que a propria API devolveu - nunca por numero de
  // pagina: a fila muda enquanto e lida (ver passo 2.1 do ROADMAP).
  if (anexar && estado.cursor) params.set("cursor", estado.cursor);

  const fila = await api(`/fila?${params}`);
  estado.itens = anexar ? [...estado.itens, ...fila.itens] : fila.itens;
  estado.cursor = fila.proximo_cursor;
  estado.total = fila.total;
  desenharFila();
}

function desenharFila() {
  $("#fila-total").textContent = `${estado.total} ${estado.total === 1 ? "caso" : "casos"}`;
  $("#fila-mais").hidden = !estado.cursor;

  if (!estado.itens.length) {
    $("#fila-lista").replaceChildren(h("li", { class: "fila-vazia" }, "Nenhum caso com estes filtros."));
    return;
  }
  $("#fila-lista").replaceChildren(...estado.itens.map(itemFila));
}

function itemFila(item) {
  const situacao = situacaoDaFila(item);
  const aberto = item.alerta_id === estado.alertaAberto;
  return h("li", {},
    h("a", {
      href: `#/alerta/${item.alerta_id}`,
      class: `item-fila situacao-borda-${situacao}${aberto ? " aberto" : ""}`,
      "aria-current": aberto ? "true" : null,
    },
      h("span", { class: "item-topo" },
        h("span", { class: "cliente" }, item.cliente_id),
        chipSituacao(situacao)),
      h("span", { class: "item-niveis" },
        chipNivel("regra", item.nivel_risco_regra),
        chipNivel("agente", item.nivel_risco_agente)),
      h("span", { class: "item-rodape" },
        h("span", {}, ROTULO_ESTADO[item.estado],
          item.analista_id ? ` · com ${item.analista_id}` : "",
          item.decisao ? ` · ${item.decisao}` : "",
          item.substitui_alerta_id ? " · dados novos" : ""),
        marcaFundamentado(item.fundamentado))));
}

function mostrarErroFila(erro) {
  $("#fila-total").textContent = "";
  $("#fila-mais").hidden = true;
  $("#fila-lista").replaceChildren(h("li", { class: "erro" }, erro.message));
}

// ---------------------------------------------------------------- caso

async function abrirCaso(alertaId) {
  if (estado.alertaAberto !== alertaId) estado.caca = null;
  estado.alertaAberto = alertaId;
  estado.marcaSelecionada = null;
  desenharFila();

  const area = $("#caso");
  area.replaceChildren(h("p", { class: "carregando" }, "Carregando caso…"));
  try {
    const [caso, evidencias] = await Promise.all([
      api(`/alertas/${alertaId}`),
      api(`/alertas/${alertaId}/evidencias`),
    ]);
    // Cliques rapidos em dois casos: a resposta do primeiro pode chegar depois
    // da do segundo. So desenha se este ainda e o caso aberto.
    if (estado.alertaAberto !== alertaId) return;
    estado.caso = caso;
    estado.evidencias = evidencias;
    area.replaceChildren(desenharCaso(caso, evidencias));
    area.scrollTop = 0;
  } catch (erro) {
    if (estado.alertaAberto !== alertaId) return;
    area.replaceChildren(h("div", { class: "erro" }, erro.message));
  }
}

function desenharCaso(caso, evidencias) {
  return h("article", { class: "caso-conteudo" },
    cabecalhoCaso(caso),
    avisoDeVigencia(caso),
    avisoDeSuspeitas(caso),
    h("div", { class: "caso-corpo" },
      colunaParecer(caso),
      colunaEvidencia(caso, evidencias)),
    blocoContraIsca(caso));
}

function cabecalhoCaso(caso) {
  const a = caso.alerta;
  const situacao = situacaoDaFila(a);
  const botoes = caso.transicoes_permitidas.map((destino) =>
    h("button", {
      type: "button",
      class: destino === "em_analise" ? "botao" : "botao botao-sec",
      onclick: () => transicionar(destino),
    }, ROTULO_TRANSICAO[destino] ?? destino));

  return h("header", { class: "caso-cab" },
    h("div", { class: "caso-titulo" },
      h("h1", {}, a.cliente_id),
      h("div", { class: "caso-chips" },
        chipNivel("regra", a.nivel_risco_regra),
        chipNivel("agente", a.nivel_risco_agente),
        chipSituacao(situacao),
        marcaFundamentado(a.fundamentado)),
      h("p", { class: "caso-sub" },
        `${a.qtd_operacoes} operações · volume ${formatarBRL(a.volume_total_brl)} · `,
        `${a.total_sinalizacoes} ${a.total_sinalizacoes === 1 ? "sinalização" : "sinalizações"}`,
        a.origem === "controle" ? " · cliente de controle (sem sinalização)" : "")),
    h("div", { class: "caso-acoes" },
      h("p", { class: "caso-estado" },
        h("span", { class: `estado estado-${a.estado}` }, ROTULO_ESTADO[a.estado]),
        a.analista_id ? h("span", { class: "dono" }, ` com ${a.analista_id}`) : null),
      botoes.length ? h("div", { class: "botoes" }, ...botoes) : null));
}

// Fase 5.1: um cliente pode ter varios alertas ao longo do tempo - um novo a
// cada lote que traz operacao dele. So o VIGENTE esta na fila; os anteriores
// ficam como historia, com a decisao que tiverem.
function avisoDeVigencia(caso) {
  const a = caso.alerta;
  if (a.substituido_por != null) {
    return h("div", { class: "alerta-caixa alerta-atencao vigencia" },
      h("strong", {}, "Este alerta foi substituído. "),
      "Chegaram operações novas para o cliente e as regras rodaram de novo. ",
      a.estado === "em_analise"
        ? "Não dá para decidir sobre a base antiga — devolva este e pegue o caso atual. "
        : "O que está aqui é a base como era. ",
      h("a", { href: `#/alerta/${a.substituido_por}` }, "Abrir o caso atual →"));
  }
  if (a.substitui_alerta_id != null) {
    const d = caso.decisao_anterior;
    return h("div", { class: "alerta-caixa alerta-info vigencia" },
      h("strong", {}, "Caso reaberto por dados novos. "),
      d
        ? `Antes das operações novas, ${d.analista_id} decidiu “${ROTULO_DECISAO[d.decisao].toLowerCase()}”`
          + `${d.nivel_risco_analista ? ` (${d.nivel_risco_analista})` : ""} em ${formatarMomento(d.decidido_em)}. `
          + "A decisão vale para a base de então; esta é uma decisão nova. "
        : "O alerta anterior deste cliente não chegou a ser decidido. ",
      h("a", { href: `#/alerta/${a.substitui_alerta_id}` }, "Ver o alerta anterior →"));
  }
  return null;
}

// ---------------------------------------------------------------- parecer

function colunaParecer(caso) {
  const p = caso.parecer;
  if (!p) {
    return h("div", { class: "coluna" },
      secao("Parecer do agente",
        h("p", { class: "vazio-bloco" },
          "Este caso ainda não foi triado. Quando o worker de triagem passar por ele, o parecer aparece aqui.")),
      blocoDecisao(caso));
  }

  const marcas = caso.aderencia?.marcas ?? [];
  const problemas = marcas.filter((m) => m.classe === "atipico_incorreto" || m.classe === "nao_encontrado");
  const confirmados = marcas.filter((m) => m.classe === "confirmado").length;

  return h("div", { class: "coluna" },
    secao("Parecer do agente",
      h("p", { class: "tipologia" }, p.tipologia_suspeita ?? ""),
      origemDoParecer(p),
      p.erro_parsing ? h("div", { class: "alerta-caixa alerta-erro" },
        h("strong", {}, "O agente não produziu um parecer válido. "), p.erro_parsing) : null,
      p.justificativa ? justificativa(p.justificativa, marcas) : null,
      h("p", { id: "explicacao", class: "explicacao", hidden: true }),
      avisoDeAderencia(caso.aderencia, problemas, confirmados),
      p.red_flags.length
        ? h("div", { class: "red-flags" },
            h("h4", {}, "Sinais apontados pelo agente"),
            h("ul", {}, ...p.red_flags.map((f) => h("li", {}, f))))
        : null),
    blocoDecisao(caso));
}

function origemDoParecer(p) {
  const partes = [`${p.modelo} · prompt ${p.versao_prompt}`];
  if (p.origem_registro === "importado_cache") {
    partes.push("registro importado do cache antigo (data inferida, não observada)");
  } else {
    partes.push(`gerado em ${formatarMomento(p.criado_em)}`);
  }
  if (p.reaproveitado_de) partes.push(`reaproveitado do parecer nº ${p.reaproveitado_de}, sem nova chamada ao modelo`);
  return h("p", { class: "meta" }, partes.join(" · "));
}

function justificativa(texto, marcas) {
  const trechos = segmentar(texto, marcas);
  return h("p", { class: "justificativa" },
    ...trechos.map(({ texto: pedaco, marca }) => {
      if (!marca) return pedaco;
      return h("button", {
        type: "button",
        class: `marca marca-${marca.classe}`,
        title: explicacaoDaMarca(marca),
        onclick: (evento) => selecionarMarca(marca, evento.currentTarget),
      }, pedaco);
    }));
}

function avisoDeAderencia(aderencia, problemas, confirmados) {
  if (!aderencia) return null;

  // Principio 2 do ROADMAP: o numero sem procedencia e MARCADO, com o motivo
  // escrito - o parecer nao e escondido.
  if (problemas.length) {
    return h("div", { class: "alerta-caixa alerta-erro" },
      h("strong", {}, problemas.length === 1 ? "Um número do parecer não tem procedência" : `${problemas.length} números do parecer não têm procedência`),
      h("ul", {}, ...problemas.map((m) =>
        h("li", {}, h("span", { class: "valor-citado" }, formatarBRL(m.valor)), " — ", explicacaoDaMarca(m)))));
  }
  if (!aderencia.fundamentado) {
    // Ex.: CLI-001 - tem sinalizacao, mas o parecer nao cita numero nenhum
    return h("div", { class: "alerta-caixa alerta-atencao" },
      h("strong", {}, "Parecer não fundamentado. "), aderencia.motivo);
  }
  if (confirmados) {
    return h("p", { class: "ok-linha" },
      `${confirmados === 1 ? "O valor citado confere" : `Os ${confirmados} valores citados conferem`} com a base do cliente. Clique em um número para ver de onde ele vem.`);
  }
  return h("p", { class: "ok-linha" }, aderencia.motivo);
}

function selecionarMarca(marca, botao) {
  const raiz = $("#caso");
  raiz.querySelectorAll(".alvo, .marca.selecionada").forEach((el) => el.classList.remove("alvo", "selecionada"));

  const explicacao = $("#explicacao");
  if (estado.marcaSelecionada === marca) {
    estado.marcaSelecionada = null;
    explicacao.hidden = true;
    return;
  }
  estado.marcaSelecionada = marca;
  botao.classList.add("selecionada");
  // Numero com problema ja tem a explicacao no aviso vermelho fixo logo abaixo
  // do texto - repeti-la aqui seria o mesmo paragrafo duas vezes seguidas.
  const temAvisoFixo = marca.classe === "atipico_incorreto" || marca.classe === "nao_encontrado";
  explicacao.textContent = explicacaoDaMarca(marca);
  explicacao.className = `explicacao explicacao-${marca.classe}`;
  explicacao.hidden = temAvisoFixo;

  const alvo = alvoDaFonte(marca.fonte);
  const alvos = [];
  if (alvo?.tipo === "agregado") {
    alvos.push(...raiz.querySelectorAll(`[data-agregado="${alvo.nome}"]`));
  } else if (alvo) {
    for (const linha of raiz.querySelectorAll("tr[data-op]")) {
      if (operacaoEhAlvo(JSON.parse(linha.dataset.op), alvo)) alvos.push(linha);
    }
    if (alvo.tipo === "dia") alvos.push(...raiz.querySelectorAll(`[data-dia="${alvo.data}"]`));
  }
  alvos.forEach((el) => el.classList.add("alvo"));

  const semMovimento = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // "center", nao "nearest": com nearest a linha de destino parava colada na
  // borda de baixo da area visivel (visto no navegador, caso CLI-028)
  alvos[0]?.scrollIntoView({ block: "center", behavior: semMovimento ? "auto" : "smooth" });
}

// ---------------------------------------------------------------- evidencia

function colunaEvidencia(caso, evidencias) {
  return h("div", { class: "coluna" },
    blocoSinalizacoes(caso.sinalizacoes),
    blocoAgregados(caso),
    blocoOperacoes(caso.operacoes),
    blocoFerramentas(evidencias),
    blocoHistorico(caso.historico_parecer, caso.parecer),
    blocoTrilha(caso.trilha));
}

function blocoSinalizacoes(sinalizacoes) {
  if (!sinalizacoes.length) {
    return secao("O que as regras apontaram",
      h("p", { class: "vazio-bloco" }, "Nenhuma regra disparou para este cliente."));
  }
  return secao("O que as regras apontaram",
    h("ul", { class: "sinalizacoes" }, ...sinalizacoes.map((s) => {
      const d = s.detalhe;
      if (s.regra === "fracionamento") {
        return h("li", { dataset: { dia: s.data } },
          h("span", { class: "regra-nome" }, "Fracionamento"),
          ` em ${formatarData(s.data)}: ${d.qtd_operacoes} operações somando ${formatarBRL(d.soma_do_dia)}, `,
          `a maior de ${formatarBRL(d.max_individual)}`);
      }
      return h("li", {},
        h("span", { class: "regra-nome" }, "Valor atípico"),
        ` ${s.operacao_id}: ${formatarBRL(d.valor_brl)}, acima do limite de ${formatarBRL(d.limite_atipico)} para este cliente`);
    })));
}

function blocoAgregados(caso) {
  // Nenhum agregado e CALCULADO aqui. O volume vem do alerta (gravado pelas
  // regras); media e mediana so aparecem quando o parecer as cita, com o valor
  // que o verificador conferiu.
  const citados = new Map();
  for (const m of caso.aderencia?.marcas ?? []) {
    const alvo = alvoDaFonte(m.fonte);
    if (alvo?.tipo === "agregado") citados.set(alvo.nome, m.valor);
  }
  const itens = [
    h("li", { dataset: { agregado: "volume" } },
      h("span", {}, "Volume total do cliente"),
      h("span", { class: "num" }, formatarBRL(caso.alerta.volume_total_brl))),
  ];
  for (const [nome, rotulo] of [["media", "Média do cliente"], ["mediana", "Mediana do cliente"]]) {
    if (citados.has(nome)) {
      itens.push(h("li", { dataset: { agregado: nome } },
        h("span", {}, rotulo, h("span", { class: "sub-rotulo" }, " (citada no parecer)")),
        h("span", { class: "num" }, formatarBRL(citados.get(nome)))));
    }
  }
  return secao("Agregados", h("ul", { class: "agregados" }, ...itens));
}

function blocoOperacoes(operacoes) {
  return secao(`Operações do cliente (${operacoes.length})`,
    h("div", { class: "tabela-rolagem" },
      h("table", { class: "operacoes" },
        h("thead", {}, h("tr", {},
          h("th", {}, "Data"), h("th", {}, "Operação"), h("th", { class: "num" }, "Valor (R$)"),
          h("th", {}, "Canal"), h("th", {}, "Tipo"), h("th", {}, "Contraparte"), h("th", {}, "Sinal"))),
        h("tbody", {}, ...operacoes.map((o) =>
          h("tr", {
            class: [o.flag_valor_atipico ? "op-atipica" : "", o.em_dia_de_fracionamento ? "op-frac" : ""].join(" ").trim() || null,
            dataset: { op: JSON.stringify({ id: o.id, data: o.data, canal: o.canal }) },
          },
            h("td", { class: "nowrap" }, formatarData(o.data)),
            h("td", { class: "mono" }, o.id),
            h("td", { class: "num" }, formatarBRL(o.valor_brl),
              o.moeda !== "BRL" ? h("span", { class: "moeda-orig" }, `${o.moeda} ${o.valor}`) : null),
            h("td", {}, o.canal),
            h("td", {}, o.tipo.replace(/_/g, " ")),
            h("td", { class: "contraparte" }, o.contraparte),
            h("td", { class: "sinais" },
              o.flag_valor_atipico ? h("span", { class: "chip chip-atipico", title: "operação sinalizada pela Regra 2" }, "atípica") : null,
              o.em_dia_de_fracionamento ? h("span", { class: "chip chip-frac", title: "dia que disparou a Regra 1" }, "dia fracionado") : null)))))));
}

function blocoFerramentas(evidencias) {
  if (!evidencias.length) {
    return secao("O que o agente consultou",
      h("p", { class: "vazio-bloco" }, "Nenhuma ferramenta registrada para este parecer."));
  }
  const semRetorno = evidencias.every((e) => e.payload === null);
  return secao("O que o agente consultou",
    semRetorno
      ? h("p", { class: "nota" },
          "O registro deste parecer guardou quais ferramentas o agente chamou, mas não o que elas devolveram — ele é anterior ao registro completo de evidências.")
      : null,
    h("ul", { class: "ferramentas" }, ...evidencias.map((e) =>
      h("li", {},
        h("code", {}, `${e.tool}(${Object.values(e.args).join(", ")})`),
        e.payload !== null
          ? h("details", {}, h("summary", {}, "ver o que a ferramenta devolveu"),
              h("pre", {}, JSON.stringify(e.payload, null, 2)))
          : null))));
}

const ROTULO_ORIGEM = {
  agente: "gerado nesta instalação",
  importado_cache: "importado do cache antigo",
};

function blocoHistorico(historico, atual) {
  if (!historico.length) {
    return secao("Histórico de pareceres", h("p", { class: "vazio-bloco" }, "Nenhum parecer ainda."));
  }
  // O append-only da Fase 1 ficando visivel: se este cliente ja recebeu outro
  // parecer, o analista ve - e ve se a data foi observada ou inferida.
  return secao(`Histórico de pareceres (${historico.length})`,
    h("ol", { class: "historico" }, ...historico.map((v) =>
      h("li", { class: v.parecer_id === atual?.parecer_id ? "atual" : null },
        h("span", { class: "hist-momento" }, formatarMomento(v.criado_em)),
        chipNivel("agente", v.nivel_risco),
        h("span", { class: "hist-origem" },
          ROTULO_ORIGEM[v.origem_registro] ?? v.origem_registro,
          // Uma copia reaproveitada tem data OBSERVADA (o momento do vinculo); o
          // registro importado original tem data INFERIDA do arquivo de cache.
          // Numa trilha de auditoria as duas nao podem parecer iguais.
          v.reaproveitado_de
            ? ` · cópia do nº ${v.reaproveitado_de}`
            : v.origem_registro === "importado_cache" ? " · data inferida" : ""),
        v.parecer_id === atual?.parecer_id ? h("span", { class: "chip chip-atual" }, "atual") : null))));
}

// ---------------------------------------------------------------- decisao (Fase 4)

function blocoDecisao(caso) {
  const a = caso.alerta;
  if (caso.decisao) return registroDecisao(caso.decisao, caso.parecer);

  const analista = $("#analista").value.trim();
  if (podeDecidir(a, analista)) return formularioDecisao(caso);

  let nota;
  if (a.substituido_por != null) {
    nota = "Alerta substituído por dados novos — a decisão é registrada no caso atual.";
  } else if (a.estado === "em_analise") {
    nota = analista
      ? `Em análise com ${a.analista_id}. Só quem pegou o caso registra a decisão.`
      : `Em análise com ${a.analista_id}. Se é você, informe seu nome no campo Analista.`;
  } else {
    nota = "Pegue o caso para registrar sua decisão.";
  }
  return secao("Decisão do analista", h("p", { class: "vazio-bloco" }, nota));
}

function registroDecisao(d, parecer) {
  const linhas = [
    h("p", { class: "decisao-titulo" },
      h("span", { class: `decisao-tipo decisao-${d.decisao}` }, ROTULO_DECISAO[d.decisao]),
      d.nivel_risco_analista ? chipNivel("analista", d.nivel_risco_analista) : null),
    h("p", { class: "meta" },
      `${d.analista_id} · ${formatarMomento(d.decidido_em)}`,
      d.parecer_id
        ? ` · sobre o parecer nº ${d.parecer_id}${d.nivel_risco_agente ? ` (agente: ${d.nivel_risco_agente})` : ""}`
        : " · decidido sem parecer do agente"),
  ];
  if (d.motivo) linhas.push(h("blockquote", { class: "decisao-motivo" }, d.motivo));
  // Registro append-only: o caso concluido nao reabre nesta fase.
  if (parecer && d.parecer_id && parecer.parecer_id !== d.parecer_id) {
    linhas.push(h("p", { class: "nota" },
      "O parecer exibido acima é mais recente do que o que o analista leu ao decidir."));
  }
  return secao("Decisão do analista", ...linhas);
}

function formularioDecisao(caso) {
  const nivelAgente = caso.parecer?.nivel_risco ?? null;
  const podeConcordar = nivelAgente !== null;

  const opcao = (valor, detalhe, desabilitada = false) =>
    h("label", { class: `opcao${desabilitada ? " opcao-off" : ""}` },
      h("input", { type: "radio", name: "decisao", value: valor, required: true, disabled: desabilitada }),
      h("span", {}, h("strong", {}, ROTULO_DECISAO[valor]), h("span", { class: "opcao-detalhe" }, detalhe)));

  const campoNivel = h("label", { class: "campo", hidden: true },
    h("span", { class: "campo-rotulo" }, "Nível de risco que você atribui"),
    h("select", { name: "nivel_risco" },
      h("option", { value: "" }, "—"),
      ...["baixo", "médio", "alto"].map((n) => h("option", { value: n }, n))));
  const campoMotivo = h("label", { class: "campo" },
    h("span", { class: "campo-rotulo" }, "Motivo"),
    h("textarea", { name: "motivo", rows: "3", maxlength: "2000" }));
  const enviar = h("button", { type: "submit", class: "botao" }, "Registrar decisão e concluir");

  const form = h("form", { class: "decisao-form" },
    h("fieldset", { class: "opcoes" },
      h("legend", { class: "campo-rotulo" }, "Sua decisão sobre o parecer"),
      opcao("concordo",
        podeConcordar ? ` — grava o nível do agente (${nivelAgente})` : " — não há nível do agente com que concordar",
        !podeConcordar),
      opcao("discordo", " — informe o nível que você atribui e o porquê"),
      opcao("escalar", " — encaminha ao segundo nível; o porquê é obrigatório")),
    campoNivel,
    campoMotivo,
    h("p", { class: "nota decisao-aviso" },
      "A decisão é definitiva: o caso é concluído e não pode ser reaberto por aqui."),
    enviar);

  // Mostra/exige os campos conforme a decisao escolhida. So apresentacao - a
  // API valida de novo e devolve o porque se recusar.
  form.addEventListener("change", (evento) => {
    if (evento.target.name !== "decisao") return;
    const campos = camposDaDecisao(evento.target.value);
    campoNivel.hidden = campos.nivel === "oculto";
    form.elements.nivel_risco.required = campos.nivel === "obrigatorio";
    form.elements.motivo.required = campos.motivo === "obrigatorio";
    campoMotivo.querySelector(".campo-rotulo").textContent =
      campos.motivo === "obrigatorio" ? "Motivo (obrigatório)" : "Motivo (opcional)";
    desarmar(enviar);
  });

  // Duas etapas, porque a acao nao se desfaz: o primeiro clique arma, o
  // segundo confirma. Sem confirm() - que some em alguns navegadores embutidos.
  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    if (enviar.dataset.armado !== "1") {
      enviar.dataset.armado = "1";
      enviar.textContent = `Confirmar: concluir ${caso.alerta.cliente_id}`;
      enviar.classList.add("botao-confirmar");
      return;
    }
    registrarDecisao(form, caso);
  });

  return secao("Sua decisão", form);
}

function desarmar(botao) {
  delete botao.dataset.armado;
  botao.textContent = "Registrar decisão e concluir";
  botao.classList.remove("botao-confirmar");
}

async function registrarDecisao(form, caso) {
  const analista = $("#analista").value.trim();
  const dados = new FormData(form);
  const decisao = dados.get("decisao");
  const campos = camposDaDecisao(decisao);
  const corpo = {
    decisao,
    // o parecer que ESTA tela mostrou - a API recusa se o atual for outro
    parecer_id: caso.parecer?.parecer_id ?? null,
    motivo: dados.get("motivo") || null,
    nivel_risco: campos.nivel === "oculto" ? null : dados.get("nivel_risco") || null,
  };
  const alertaId = caso.alerta.alerta_id;
  const botao = form.querySelector("button[type=submit]");
  botao.disabled = true;
  try {
    await api(`/alertas/${alertaId}/decisao`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Analista": analista },
      body: JSON.stringify(corpo),
    });
    avisar(`${caso.alerta.cliente_id} concluído: ${ROTULO_DECISAO[decisao].toLowerCase()}.`, "ok");
  } catch (erro) {
    avisar(erro.message, "erro");
    botao.disabled = false;
    desarmar(botao);
    if (erro.status !== 409) return; // 422: o formulario fica como estava para corrigir
  }
  // sucesso ou 409 (o caso mudou): recarrega do servidor
  await Promise.all([carregarFila().catch(mostrarErroFila), abrirCaso(alertaId)]);
}

const ROTULO_PASSO = {
  "novo>triado": "triado pelo agente",
  "novo>em_analise": "pegou o caso (antes da triagem)",
  "triado>em_analise": "pegou o caso",
  "em_analise>triado": "devolveu para a fila",
  "em_analise>concluido": "concluiu com decisão",
};

function blocoTrilha(trilha) {
  if (!trilha.length) {
    return secao("Trilha do caso",
      h("p", { class: "vazio-bloco" },
        "Nenhuma transição registrada. A trilha começa a ser gravada no esquema v6; mudanças anteriores não foram guardadas."));
  }
  return secao(`Trilha do caso (${trilha.length})`,
    h("ol", { class: "historico trilha" }, ...trilha.map((t) =>
      h("li", {},
        h("span", { class: "hist-momento" }, formatarMomento(t.registrado_em)),
        h("span", { class: t.ator_tipo === "sistema" ? "ator ator-sistema" : "ator" }, t.ator),
        h("span", {}, ROTULO_PASSO[`${t.estado_anterior}>${t.estado_novo}`]
          ?? `${ROTULO_ESTADO[t.estado_anterior]} → ${ROTULO_ESTADO[t.estado_novo]}`)))));
}

// ---------------------------------------------------------------- contra-isca (Fase 6)
//
// O caminhao dos 30 kg era verdade - e era isca. Enquanto o analista olha o
// caso chamativo, o que passou AO LADO dele, em outros clientes? A caca e da
// API (mesa/contra_isca.py); aqui so se desenha, e cada ligacao aparece com a
// frase de onde vem cada parte do escore.

// Quem ja ligou ESTE cliente a uma isca. E o aviso que muda o trabalho: o
// caso pode estar "sem sinal", e alguem ja o apontou a partir de outro caso.
function avisoDeSuspeitas(caso) {
  const sobre = caso.suspeitas_sobre_o_cliente ?? [];
  if (!sobre.length) return null;
  return h("div", { class: "alerta-caixa alerta-atencao suspeitas-aviso" },
    h("strong", {}, sobre.length === 1 ? "Suspeita registrada sobre este cliente. "
                                       : `${sobre.length} suspeitas registradas sobre este cliente. `),
    h("ul", {}, ...sobre.map((s) =>
      h("li", {},
        `${s.analista_id}, em ${formatarMomento(s.registrado_em)}, a partir do caso `,
        h("a", { href: `#/alerta/${s.alerta_origem_id}` }, s.cliente_origem),
        ` (${s.operacoes.join(", ")}): `,
        h("q", {}, s.motivo)))));
}

function blocoContraIsca(caso) {
  const conteudo = h("div", { id: "caca" });
  const caca = estado.caca?.alerta_id === caso.alerta.alerta_id ? estado.caca : null;
  conteudo.append(caca ? resultadoDaCaca(caca, caso) : convidarACacar(caso));
  return h("section", { class: "bloco caca" },
    h("h3", {}, "O que passou ao lado deste caso"), conteudo);
}

function convidarACacar(caso) {
  return h("div", { class: "caca-convite" },
    h("p", { class: "nota" },
      "As regras olham um cliente por vez. Fracionamento espalhado por vários clientes, ou um volume grande ",
      "que entra enquanto este caso está em análise, não dispara regra nenhuma. A caça procura operações de ",
      "outros clientes ligadas a este pela contraparte — perto no tempo ou durante a análise. Só consulta: ",
      "não grava nada."),
    h("button", { type: "button", class: "botao", onclick: () => cacar(caso.alerta.alerta_id) }, ROTULO_CACA));
}

async function cacar(alertaId) {
  const alvo = $("#caca");
  if (alvo) alvo.replaceChildren(h("p", { class: "carregando" }, "Caçando…"));
  try {
    const caca = await api(`/alertas/${alertaId}/contra-isca`);
    if (estado.alertaAberto !== alertaId) return;
    estado.caca = caca;
    $("#caca")?.replaceChildren(resultadoDaCaca(caca, estado.caso));
  } catch (erro) {
    if (estado.alertaAberto !== alertaId) return;
    $("#caca")?.replaceChildren(h("div", { class: "erro" }, erro.message));
  }
}

function resultadoDaCaca(caca, caso) {
  const grupos = agruparLigacoes(caca.ligacoes);
  const p = caca.parametros;
  const periodos = caca.periodos_em_analise;
  const resumo = h("p", { class: "caca-resumo" },
    grupos.length
      ? `${caca.ligacoes.length} ${caca.ligacoes.length === 1 ? "operação" : "operações"} em `
        + `${grupos.length} ${grupos.length === 1 ? "cliente" : "clientes"}, da ligação mais forte para a mais fraca. `
      : "Nenhuma operação de outro cliente ligada a este caso. ",
    `Janela de ±${p.janela_dias} dias; limite individual de ${formatarBRL(caca.limites.frac_max_individual)} `,
    `(execução ${caca.execucao_id}); `,
    periodos.length
      ? `${periodos.length === 1 ? "período" : "períodos"} em análise: `
        + periodos.map((x) => `${formatarMomento(x.inicio)} → ${x.fim ? formatarMomento(x.fim) : "agora"} (${x.analista})`).join("; ")
        + "."
      : "o caso nunca esteve em análise, então só a janela de dias conta.",
    h("button", { type: "button", class: "botao botao-sec botao-mini", onclick: () => cacar(caca.alerta_id) },
      "Caçar de novo"));
  if (!grupos.length) return h("div", {}, resumo);

  return h("div", { class: "caca-resultado" },
    resumo,
    mapaDaCaca(caca.cliente_id, grupos),
    h("p", { class: "nota" },
      "O sistema caça, você decide: uma ligação só vira registro quando um analista assina a suspeita, com o motivo."),
    h("ol", { class: "caca-grupos" }, ...grupos.map((g) => grupoDaCaca(g, caca, caso))));
}

const SVG = "http://www.w3.org/2000/svg";
function s(tag, atributos, ...filhos) {
  const el = document.createElementNS(SVG, tag);
  for (const [chave, valor] of Object.entries(atributos ?? {})) {
    if (valor == null) continue;
    if (chave.startsWith("on")) el.addEventListener(chave.slice(2), valor);
    else el.setAttribute(chave, valor);
  }
  for (const filho of filhos.flat()) {
    if (filho == null) continue;
    el.append(filho instanceof Node ? filho : document.createTextNode(String(filho)));
  }
  return el;
}

const MAX_NO_MAPA = 12;

// A isca no centro, os clientes ligados em volta. Espessura = escore (relativo
// ao mais forte). Cor = padrao. Clicar num cliente leva a ligacao na lista.
function mapaDaCaca(isca, grupos) {
  const visiveis = grupos.slice(0, MAX_NO_MAPA);
  const L = 640, A = 340, cx = L / 2, cy = A / 2;
  const pos = posicoesNoMapa(visiveis.length, cx, cy, 128);
  const maior = visiveis[0]?.escore ?? 0;
  const irPara = (cliente) => {
    const alvo = document.getElementById(`grupo-${cliente}`);
    if (!alvo) return;
    alvo.scrollIntoView({ behavior: "smooth", block: "center" });
    alvo.classList.remove("piscando");
    void alvo.offsetWidth;  // reinicia a animacao
    alvo.classList.add("piscando");
  };
  const classePadrao = (g) => `no-${g.padroes.join("").toLowerCase()}`;

  const linhas = visiveis.map((g, i) => s("line", {
    x1: cx, y1: cy, x2: pos[i].x, y2: pos[i].y, class: `aresta ${classePadrao(g)}`,
    "stroke-width": espessura(g.escore, maior).toFixed(2),
  }));
  const nos = visiveis.map((g, i) => s("g", {
    class: `no ${classePadrao(g)}`, tabindex: "0", role: "button",
    "aria-label": `${g.cliente_id}: escore ${g.escore.toFixed(2)}, ${g.padroes.map((x) => ROTULO_PADRAO[x]).join(" e ")}`,
    onclick: () => irPara(g.cliente_id),
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); irPara(g.cliente_id); } },
  },
    s("title", {}, `${g.cliente_id} · escore ${g.escore.toFixed(2)}`),
    s("circle", { cx: pos[i].x, cy: pos[i].y, r: 25 }),
    s("text", { x: pos[i].x, y: pos[i].y - 2, "text-anchor": "middle", class: "no-nome" }, g.cliente_id.replace(/^CLI-/, "")),
    s("text", { x: pos[i].x, y: pos[i].y + 11, "text-anchor": "middle", class: "no-escore" }, g.escore.toFixed(2))));

  return h("figure", { class: "mapa" },
    s("svg", { viewBox: `0 0 ${L} ${A}`, role: "group", "aria-label": `Clientes ligados a ${isca}` },
      ...linhas,
      s("g", { class: "no no-isca" },
        s("circle", { cx, cy, r: 36 }),
        s("text", { x: cx, y: cy - 3, "text-anchor": "middle", class: "no-nome" }, isca),
        s("text", { x: cx, y: cy + 12, "text-anchor": "middle", class: "no-escore" }, "o caso")),
      ...nos),
    h("figcaption", { class: "mapa-legenda" },
      h("span", { class: "legenda no-a" }, ROTULO_PADRAO.A),
      h("span", { class: "legenda no-b" }, ROTULO_PADRAO.B),
      h("span", { class: "legenda no-ab" }, "os dois"),
      h("span", {}, "número = escore; espessura da linha = força da ligação"
        + (grupos.length > MAX_NO_MAPA ? `; no mapa, os ${MAX_NO_MAPA} mais fortes de ${grupos.length}` : ""))));
}

function grupoDaCaca(g, caca, caso) {
  const registradas = suspeitasDoCliente(caca.suspeitas, g.cliente_id);
  // o formulario some so quando as suspeitas ja cobrem TODAS as operacoes de
  // agora - a base cresce, e uma ligacao nova do mesmo cliente pode aparecer
  const cobertas = new Set(registradas.flatMap((x) => x.operacoes));
  const tudoCoberto = g.ligacoes.every((l) => cobertas.has(l.operacao_id));
  return h("li", { class: "grupo-caca", id: `grupo-${g.cliente_id}` },
    h("header", { class: "grupo-cab" },
      h("span", { class: "cliente" }, g.cliente_id),
      ...g.padroes.map((x) => h("span", { class: `chip chip-padrao no-${x.toLowerCase()}` }, ROTULO_PADRAO[x])),
      h("span", { class: "grupo-escore", title: "escore da ligação mais forte deste cliente" },
        `escore ${g.escore.toFixed(2)}`),
      g.alerta_do_cliente != null
        ? h("a", { class: "grupo-link", href: `#/alerta/${g.alerta_do_cliente}` }, "abrir o caso →")
        : null),
    h("ul", { class: "ligacoes" }, ...g.ligacoes.map((l) =>
      h("li", { class: "ligacao" },
        h("p", { class: "ligacao-linha" },
          h("span", { class: "nowrap" }, formatarData(l.data)),
          h("span", { class: "mono" }, l.operacao_id),
          h("span", { class: "num" }, formatarBRL(l.valor_brl)),
          h("span", { class: "contraparte" }, l.contraparte),
          h("span", { class: "ligacao-escore" }, l.escore.toFixed(2))),
        h("ul", { class: "componentes" }, ...l.componentes.map((c) =>
          h("li", {},
            h("span", { class: `comp-nome comp-${c.nome}` }, ROTULO_COMPONENTE[c.nome] ?? c.nome),
            h("span", { class: "comp-valor" }, c.nome === "contraparte" ? `× ${c.valor.toFixed(2)}` : `+ ${c.valor.toFixed(2)}`),
            h("span", { class: "comp-frase" }, c.procedencia))))))),
    registradas.length
      ? h("ul", { class: "suspeitas-registradas" }, ...registradas.map((x) =>
          h("li", {}, h("strong", {}, "Suspeita registrada"),
            ` por ${x.analista_id} em ${formatarMomento(x.registrado_em)} (${x.operacoes.join(", ")}): `,
            h("q", {}, x.motivo))))
      : null,
    tudoCoberto ? null : formularioSuspeita(g, caca, caso));
}

function formularioSuspeita(g, caca, caso) {
  const enviar = h("button", { type: "submit", class: "botao" }, "Registrar suspeita");
  const form = h("form", { class: "suspeita-form" },
    h("label", { class: "campo" },
      h("span", { class: "campo-rotulo" }, `Por que ${g.cliente_id} é suspeito? (obrigatório)`),
      h("textarea", { name: "motivo", rows: "2", maxlength: "2000", required: true })),
    h("p", { class: "nota" },
      `Registra ${g.ligacoes.length === 1 ? "esta operação" : `estas ${g.ligacoes.length} operações`} `,
      "com o retrato da caça de agora. Não cria alerta nem muda o caso; não se apaga."),
    enviar);
  form.addEventListener("input", () => {
    delete enviar.dataset.armado;
    enviar.textContent = "Registrar suspeita";
    enviar.classList.remove("botao-confirmar");
  });
  // duas etapas, como a decisao: o registro e append-only
  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    const analista = $("#analista").value.trim();
    if (!analista) {
      avisar("Informe seu nome no campo Analista antes de registrar uma suspeita.", "erro");
      $("#analista").focus();
      return;
    }
    if (enviar.dataset.armado !== "1") {
      enviar.dataset.armado = "1";
      enviar.textContent = `Confirmar: suspeita sobre ${g.cliente_id}`;
      enviar.classList.add("botao-confirmar");
      return;
    }
    registrarSuspeita(form, g, caca, analista);
  });
  return form;
}

async function registrarSuspeita(form, g, caca, analista) {
  const botao = form.querySelector("button[type=submit]");
  botao.disabled = true;
  const alertaId = caca.alerta_id;
  try {
    await api(`/alertas/${alertaId}/suspeitas`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Analista": analista },
      body: JSON.stringify({
        cliente_id: g.cliente_id,
        operacoes: g.ligacoes.map((l) => l.operacao_id),
        motivo: new FormData(form).get("motivo"),
      }),
    });
    avisar(`Suspeita sobre ${g.cliente_id} registrada.`, "ok");
  } catch (erro) {
    avisar(erro.message, "erro");
    botao.disabled = false;
    if (erro.status !== 409) return;
  }
  // a caca de novo: a suspeita aparece no grupo, e o retrato e o do servidor
  await cacar(alertaId);
}

// ---------------------------------------------------------------- metricas (Fase 4.2 / 4.3)

const NIVEIS = ["baixo", "médio", "alto"];

// O botao do topo mostra onde o analista esta: ativo nas metricas.
function marcarRota() {
  const nasMetricas = location.hash === "#/metricas";
  const link = $("#link-metricas");
  link.classList.toggle("ativo", nasMetricas);
  if (nasMetricas) link.setAttribute("aria-current", "page");
  else link.removeAttribute("aria-current");
}

async function abrirMetricas() {
  estado.alertaAberto = null;
  estado.caso = null;
  // trocar so o # nao recarrega a pagina: sem isto, a fila ao lado mostraria
  // estados de antes das decisoes que as metricas ja contam
  carregarFila().catch(mostrarErroFila);
  const area = $("#caso");
  area.replaceChildren(h("p", { class: "carregando" }, "Carregando métricas…"));

  const linhaDeBase = lerLinhaDeBase();
  const params = new URLSearchParams();
  if (linhaDeBase) params.set("linha_de_base_min", linhaDeBase);
  try {
    const m = await api(`/metricas?${params}`);
    if (alertaDaRota() !== null || location.hash !== "#/metricas") return;
    area.replaceChildren(desenharMetricas(m, linhaDeBase));
  } catch (erro) {
    area.replaceChildren(h("div", { class: "erro" }, erro.message));
  }
}

const CHAVE_LINHA_DE_BASE = "mesa-triagem.linha-de-base-min";
function lerLinhaDeBase() {
  try { return localStorage.getItem(CHAVE_LINHA_DE_BASE) ?? ""; } catch { return ""; }
}
function gravarLinhaDeBase(valor) {
  try {
    if (valor) localStorage.setItem(CHAVE_LINHA_DE_BASE, valor);
    else localStorage.removeItem(CHAVE_LINHA_DE_BASE);
  } catch { /* segue sem lembrar */ }
}

function numeroGrande(rotulo, valor, detalhe) {
  return h("div", { class: "numero" },
    h("span", { class: "numero-valor" }, valor),
    h("span", { class: "numero-rotulo" }, rotulo),
    detalhe ? h("span", { class: "numero-detalhe" }, detalhe) : null);
}

function tabelaMatriz(matriz, linhas, colunas) {
  return h("div", { class: "tabela-rolagem" },
    h("table", { class: "matriz" },
      h("thead", {}, h("tr", {},
        h("th", {}, `${linhas} ↓ · ${colunas} →`),
        ...NIVEIS.map((n) => h("th", { class: "num" }, n)))),
      h("tbody", {}, ...NIVEIS.map((l) =>
        h("tr", {},
          h("th", {}, l),
          ...NIVEIS.map((c) => h("td", {
            class: `num${l === c ? " diagonal" : ""}${matriz[l][c] ? "" : " zero"}`,
          }, String(matriz[l][c]))))))));
}

function desenharMetricas(m, linhaDeBase) {
  const av = m.agente_vs_analista;
  const semDecisao = m.decididos === 0;

  const cab = h("header", { class: "caso-cab" },
    h("div", { class: "caso-titulo" },
      h("h1", {}, "Métricas da mesa"),
      h("p", { class: "caso-sub" },
        // sem execucao_id a API mede todos os casos vigentes (Fase 5.1)
        m.execucao_id == null ? "Casos vigentes · " : `Execução ${m.execucao_id} · `,
        `${m.decididos} de ${m.casos} casos decididos · `,
        `${m.por_decisao.concordo} concordo · ${m.por_decisao.discordo} discordo · ${m.por_decisao.escalar} escalar`)));

  if (semDecisao) {
    return h("article", { class: "caso-conteudo" }, cab,
      secao("Agente x analista",
        h("p", { class: "vazio-bloco" },
          "Nenhum caso decidido ainda. As métricas aparecem com a primeira decisão registrada — sem decisão, não há o que medir (e “0%” seria uma afirmação falsa).")));
  }

  const ad = m.aderencia_vs_decisao;
  const linhaAderencia = (rotulo, g) => h("tr", {},
    h("th", {}, rotulo),
    h("td", { class: "num" }, String(g.decididos)),
    h("td", { class: "num" }, String(g.concordo)),
    h("td", { class: "num" }, String(g.discordo)),
    h("td", { class: "num" }, String(g.escalar)),
    h("td", { class: "num" }, formatarPercentual(g.taxa_de_rejeicao)));

  const t = m.tempo;
  const campoBase = h("input", {
    type: "number", min: "1", max: "1440", step: "1", value: linhaDeBase || null,
    placeholder: "min", class: "campo-base", "aria-label": "Linha de base em minutos",
  });
  const formBase = h("form", { class: "form-base" },
    h("label", {}, "Tempo por caso SEM a ferramenta: ", campoBase, " min"),
    h("button", { type: "submit", class: "botao botao-sec" }, "Aplicar"));
  formBase.addEventListener("submit", (evento) => {
    evento.preventDefault();
    gravarLinhaDeBase(campoBase.value.trim());
    abrirMetricas();
  });

  return h("article", { class: "caso-conteudo" }, cab,
    h("div", { class: "caso-corpo" },
      h("div", { class: "coluna" },
        secao("Agente x analista",
          h("div", { class: "numeros" },
            numeroGrande("mesmo nível de risco", formatarPercentual(av.concordancia),
              `${av.comparaveis} casos comparáveis`),
            numeroGrande("parecer aceito como está", formatarPercentual(av.aceitacao_do_parecer),
              `concordo, de ${av.decididos_com_parecer} com parecer`)),
          tabelaMatriz(av.matriz, "agente", "analista"),
          h("p", { class: "nota" },
            "Casos escalados sem nível ficam fora da comparação de nível — não são contados como divergência.")),
        secao("A métrica antiga, nos mesmos casos",
          h("div", { class: "numeros" },
            numeroGrande("regra x analista", formatarPercentual(m.regra_vs_analista.concordancia),
              `${m.regra_vs_analista.comparaveis} comparáveis`),
            numeroGrande("regra x agente", formatarPercentual(m.regra_vs_agente.concordancia),
              `${m.regra_vs_agente.comparaveis} comparáveis`)),
          h("p", { class: "nota" },
            "Regra x agente compara duas máquinas. Com decisão humana registrada, a referência passa a ser o analista."))),
      h("div", { class: "coluna" },
        secao("O verificador acerta o que o analista rejeita?",
          h("div", { class: "tabela-rolagem" },
            h("table", { class: "matriz" },
              h("thead", {}, h("tr", {},
                h("th", {}, "parecer"), h("th", { class: "num" }, "decididos"),
                h("th", { class: "num" }, "concordo"), h("th", { class: "num" }, "discordo"),
                h("th", { class: "num" }, "escalar"), h("th", { class: "num" }, "rejeição"))),
              h("tbody", {},
                linhaAderencia("números conferem", ad.fundamentado),
                linhaAderencia("número sem procedência", ad.nao_fundamentado),
                ad.sem_verificacao.decididos ? linhaAderencia("sem verificação", ad.sem_verificacao) : null))),
          h("p", { class: "nota" }, "Rejeição = discordo + escalar.")),
        secao("Tempo de análise",
          h("div", { class: "numeros" },
            numeroGrande("mediana por caso", formatarDuracao(t.mediana_s),
              `${t.casos_medidos} casos medidos pela trilha`),
            numeroGrande("economia por caso",
              t.economia_mediana_s == null ? "—"
                : t.economia_mediana_s < 0 ? `−${formatarDuracao(-t.economia_mediana_s)}`
                : formatarDuracao(t.economia_mediana_s),
              t.linha_de_base ? `vs. ${t.linha_de_base.minutos} min informados` : "informe a linha de base")),
          t.casos_sem_trilha_completa
            ? h("p", { class: "nota" },
                t.casos_sem_trilha_completa === 1
                  ? "1 caso decidido não tem trilha completa (pego antes do esquema v6) e não entra na conta."
                  : `${t.casos_sem_trilha_completa} casos decididos não têm trilha completa (pegos antes do esquema v6) e não entram na conta.`)
            : null,
          formBase,
          h("p", { class: "nota" },
            t.linha_de_base
              ? `Linha de base ${t.linha_de_base.procedencia}.`
              : "O sistema mede o tempo COM a ferramenta. O tempo SEM ela não está no banco — a economia só é calculada com a linha de base que você informar, e aparece marcada como tal."),
          t.economia_mediana_s != null && t.economia_mediana_s < 0
            ? h("p", { class: "alerta-caixa alerta-atencao" },
                "A mediana medida é MAIOR que a linha de base: neste recorte, a ferramenta está custando tempo.")
            : null))));
}

// ---------------------------------------------------------------- acoes

async function transicionar(destino) {
  const analista = $("#analista").value.trim();
  if (!analista) {
    avisar("Informe seu nome no campo Analista antes de pegar ou devolver um caso.", "erro");
    $("#analista").focus();
    return;
  }
  const alertaId = estado.alertaAberto;
  const cliente = estado.caso?.alerta.cliente_id ?? `#${alertaId}`;
  try {
    await api(`/alertas/${alertaId}/estado`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Analista": analista },
      body: JSON.stringify({ estado: destino }),
    });
    avisar(destino === "em_analise" ? `${cliente} agora está com você.` : `${cliente} voltou para a fila.`, "ok");
  } catch (erro) {
    // 409 ja vem com a explicacao da API (o que era permitido, ou que outro
    // analista pegou o caso antes) - mostrar como veio.
    avisar(erro.message, "erro");
  }
  await Promise.all([carregarFila().catch(mostrarErroFila), abrirCaso(alertaId)]);
}

// ---------------------------------------------------------------- inicio

function alertaDaRota() {
  const m = location.hash.match(/^#\/alerta\/(\d+)$/);
  return m ? Number(m[1]) : null;
}

async function iniciar() {
  const campo = $("#analista");
  campo.value = lerAnalista();
  campo.addEventListener("input", () => {
    gravarAnalista(campo.value.trim());
    // o formulario de decisao depende de quem esta olhando: redesenha o caso
    // aberto (sem ir a API) quando o nome muda
    if (estado.caso && estado.caso.alerta.alerta_id === estado.alertaAberto) {
      $("#caso").replaceChildren(desenharCaso(estado.caso, estado.evidencias));
    }
  });

  $("#filtro-estado").addEventListener("change", () => carregarFila().catch(mostrarErroFila));
  $("#filtro-origem").addEventListener("change", () => carregarFila().catch(mostrarErroFila));
  // Clicar em Metricas estando nas metricas nao muda o # (nenhum hashchange):
  // sem isto o clique pareceria morto. Aqui ele recarrega os numeros.
  $("#link-metricas").addEventListener("click", () => {
    if (location.hash === "#/metricas") abrirMetricas();
  });
  $("#fila-mais").addEventListener("click", () => carregarFila({ anexar: true }).catch(mostrarErroFila));
  window.addEventListener("hashchange", () => {
    marcarRota();
    if (location.hash === "#/metricas") return abrirMetricas();
    const id = alertaDaRota();
    if (id) abrirCaso(id);
  });

  // Deploy de demonstracao (MESA_DEMO=1): a faixa no topo. Falhar aqui nunca
  // impede a tela de abrir - a fila mostra o erro da API por conta propria.
  api("/saude").then((s) => { if (s.demo) $("#faixa-demo").hidden = false; }).catch(() => {});

  try {
    await carregarFila();
  } catch (erro) {
    mostrarErroFila(erro);
  }
  marcarRota();
  if (location.hash === "#/metricas") return abrirMetricas();
  const id = alertaDaRota();
  if (id) abrirCaso(id);
}

iniciar();
