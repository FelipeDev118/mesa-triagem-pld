// Logica pura da tela - nenhum acesso a DOM nem a rede, para poder ser testada
// com `node --test` (tests/web/logica.test.mjs).
//
// Regra da Fase 3: a tela NAO calcula regra nenhuma. Tudo que ela marca vem da
// API, que vem do store. As funcoes aqui so FORMATAM e ORGANIZAM o que chegou:
// quebrar o texto nas posicoes que o verificador gravou, traduzir o nome de uma
// fonte para algo legivel, decidir a cor de uma linha. Nenhuma procura numero
// no texto - isso ja foi feito, uma vez, em Python.

const BRL = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });

export function formatarBRL(valor) {
  return valor == null ? "—" : BRL.format(valor);
}

// "2026-05-26" -> "26/05/2026". Por split, nao por Date: new Date("2026-05-26")
// e meia-noite UTC, que no fuso de Brasilia vira o dia 25.
export function formatarData(iso) {
  if (!iso) return "sem data";
  const [ano, mes, dia] = iso.slice(0, 10).split("-");
  return `${dia}/${mes}/${ano}`;
}

// "2026-09-12T17:00:49Z" -> "12/09/2026 17:00 UTC". O fuso fica explicito: um
// registro de auditoria que muda de hora conforme quem le nao serve.
export function formatarMomento(iso) {
  if (!iso) return "—";
  return `${formatarData(iso)} ${iso.slice(11, 16)} UTC`;
}

// Quebra a justificativa em trechos, nas posicoes que o verificador gravou.
// Devolve [{texto, marca}] em ordem; marca = null para texto corrido.
//
// Defensivo com marcas ruins (fora do texto, invertidas ou sobrepostas): elas
// sao IGNORADAS, e o texto sai inteiro de qualquer jeito. Perder uma marcacao e
// aceitavel; perder um pedaco do parecer que o analista precisa ler, nao.
export function segmentar(texto, marcas) {
  texto = texto ?? "";
  const validas = (marcas ?? [])
    .filter((m) => Number.isInteger(m.inicio) && Number.isInteger(m.fim))
    .filter((m) => m.inicio >= 0 && m.fim <= texto.length && m.inicio < m.fim)
    .sort((a, b) => a.inicio - b.inicio);

  const trechos = [];
  let cursor = 0;
  for (const marca of validas) {
    if (marca.inicio < cursor) continue; // sobreposta a anterior
    if (marca.inicio > cursor) trechos.push({ texto: texto.slice(cursor, marca.inicio), marca: null });
    trechos.push({ texto: texto.slice(marca.inicio, marca.fim), marca });
    cursor = marca.fim;
  }
  if (cursor < texto.length) trechos.push({ texto: texto.slice(cursor), marca: null });
  return trechos;
}

// Traduz o nome de referencia que o verificador gravou para o que a tela deve
// destacar. Os nomes vem de _referencias_validas() em verificacao_aderencia.py.
export function alvoDaFonte(fonte) {
  if (!fonte) return null;
  let m;
  if ((m = fonte.match(/^operacao (.+)$/))) return { tipo: "operacao", id: m[1] };
  if ((m = fonte.match(/^soma_do_dia_(\d{4}-\d{2}-\d{2})$/))) return { tipo: "dia", data: m[1] };
  if ((m = fonte.match(/^soma_canal_(.+)$/))) return { tipo: "canal", canal: m[1] };
  const agregados = {
    volume_total_cliente: "volume",
    media_cliente: "media",
    mediana_cliente: "mediana",
  };
  if (fonte in agregados) return { tipo: "agregado", nome: agregados[fonte] };
  return null;
}

const NOMES_AGREGADO = {
  volume: "volume total do cliente",
  media: "média do cliente",
  mediana: "mediana do cliente",
};

export function rotuloDaFonte(fonte) {
  const alvo = alvoDaFonte(fonte);
  if (!alvo) return fonte ?? "";
  switch (alvo.tipo) {
    case "operacao": return `operação ${alvo.id}`;
    case "dia": return `soma do dia ${formatarData(alvo.data)}`;
    case "canal": return `soma do canal ${alvo.canal.toUpperCase()}`;
    case "agregado": return NOMES_AGREGADO[alvo.nome];
  }
}

// Uma operacao e "alvo" de uma marca? E o que liga o numero clicado a linha da
// tabela. Agregados (media, mediana) nao apontam para operacao nenhuma - e
// justamente o erro que o verificador ja cometeu uma vez (confundir a mediana
// com a operacao do meio da distribuicao).
export function operacaoEhAlvo(operacao, alvo) {
  if (!alvo) return false;
  switch (alvo.tipo) {
    case "operacao": return operacao.id === alvo.id;
    case "dia": return operacao.data === alvo.data;
    case "canal": return operacao.canal === alvo.canal;
    default: return false;
  }
}

// O que dizer ao analista sobre cada classe de marca. Escrito para quem le o
// caso, nao para quem conhece o verificador.
export function explicacaoDaMarca(marca) {
  switch (marca.classe) {
    case "confirmado":
      return `Confere com ${rotuloDaFonte(marca.fonte)}.`;
    case "atipico_incorreto":
      return `Este valor existe (${rotuloDaFonte(marca.fonte)}), mas NÃO é uma das operações sinalizadas como atípicas. O parecer o usa no contexto errado.`;
    case "nao_encontrado":
      return "Este valor não corresponde a nenhuma operação, soma diária ou agregado deste cliente.";
    case "limiar":
      return "Faixa ou limite aproximado, não um valor específico - não é conferido.";
    default:
      return "";
  }
}

// Estado visual de uma linha da fila. "nao_triado" tem visual PROPRIO: nao e
// "diverge". Confundir os dois foi um erro real que o confronto.py teve que
// desfazer - a API devolve null exatamente para esta distincao.
export function situacaoDaFila(item) {
  if (item.concorda === null || item.concorda === undefined) return "nao_triado";
  return item.concorda ? "concorda" : "diverge";
}

// Rotulo do botao para cada transicao que a API disser que e permitida. A tela
// nao decide QUAIS aparecem - so como se chamam.
export const ROTULO_TRANSICAO = {
  em_analise: "Pegar caso",
  triado: "Devolver para a fila",
};

export const ROTULO_ESTADO = {
  novo: "novo",
  triado: "triado",
  em_analise: "em análise",
  concluido: "concluído",
};

// ---------------------------------------------------------------- Fase 4

export const ROTULO_DECISAO = {
  concordo: "Concordo com o parecer",
  discordo: "Discordo",
  escalar: "Escalar",
};

// Quais campos o formulario de decisao mostra, para cada decisao. E so
// APRESENTACAO: quem valida e a API (e o schema, por baixo). Se as duas
// divergirem, o pior caso e a API recusar com 422 e a tela mostrar o porque -
// nunca uma decisao invalida gravada.
//   nivel : "oculto" (concordo grava o nivel do agente) | "obrigatorio" | "opcional"
//   motivo: "obrigatorio" | "opcional"
export function camposDaDecisao(decisao) {
  switch (decisao) {
    case "concordo": return { nivel: "oculto", motivo: "opcional" };
    case "discordo": return { nivel: "obrigatorio", motivo: "obrigatorio" };
    case "escalar": return { nivel: "opcional", motivo: "obrigatorio" };
    default: return { nivel: "oculto", motivo: "opcional" };
  }
}

// O formulario so aparece para o DONO do caso em analise. A API recusa os
// outros com 409; a tela nao oferece um formulario que so pode falhar.
// Comparacao exata, como a API faz com o X-Analista (com trim).
// Alerta substituido (chegou operacao nova, Fase 5.1) tambem nao: a API recusa
// decidir sobre a base velha.
export function podeDecidir(alerta, analista) {
  const nome = (analista ?? "").trim();
  return alerta.estado === "em_analise" && nome !== "" && alerta.analista_id === nome
    && alerta.substituido_por == null;
}

// Duracao em segundos -> "1 h 05 min", "12 min", "40 s". null -> "—".
export function formatarDuracao(segundos) {
  if (segundos == null) return "—";
  const s = Math.round(segundos);
  if (s < 60) return `${s} s`;
  const min = Math.round(s / 60);
  if (min < 60) return `${min} min`;
  return `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min`;
}

// Fracao 0..1 -> "76,7%". null (denominador zero, na API) -> "—", NUNCA "0%":
// "sem dado" e "zero por cento" sao afirmacoes diferentes.
export function formatarPercentual(fracao) {
  if (fracao == null) return "—";
  return `${(fracao * 100).toLocaleString("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
}

// ---------------------------------------------------------------- Fase 6

// O nome do botao (decisao E1 do ROADMAP): diz o que faz. NAO "falso
// positivo" - em PLD isso ja quer dizer "alerta que se mostrou legitimo", o
// contrario da ideia.
export const ROTULO_CACA = "Caçar o que passou";

export const ROTULO_PADRAO = {
  A: "fracionamento distribuído",
  B: "passou durante a análise",
};

export const ROTULO_COMPONENTE = {
  contraparte: "peso da contraparte",
  janela: "perto no tempo",
  faixa: "logo abaixo do limite",
  distribuicao: "vários clientes",
  analise: "durante a análise",
  volume: "volume",
};

// As ligacoes (uma por operacao) agrupadas por cliente, para o mapa e para a
// suspeita - que e registrada por cliente. Ordem: o cliente com a ligacao mais
// forte primeiro; dentro dele, a ordem da API (escore). Empate: cliente_id,
// para a tela nao reordenar entre duas cacas iguais.
export function agruparLigacoes(ligacoes) {
  const grupos = new Map();
  for (const l of ligacoes) {
    let g = grupos.get(l.cliente_id);
    if (!g) {
      g = { cliente_id: l.cliente_id, alerta_do_cliente: l.alerta_do_cliente ?? null,
            escore: 0, padroes: [], ligacoes: [] };
      grupos.set(l.cliente_id, g);
    }
    g.ligacoes.push(l);
    g.escore = Math.max(g.escore, l.escore);
    for (const p of l.padroes) if (!g.padroes.includes(p)) g.padroes.push(p);
  }
  for (const g of grupos.values()) g.padroes.sort();
  return [...grupos.values()].sort((a, b) =>
    b.escore - a.escore || a.cliente_id.localeCompare(b.cliente_id));
}

// As suspeitas ja registradas sobre um cliente, a partir desta caca.
export function suspeitasDoCliente(suspeitas, clienteId) {
  return (suspeitas ?? []).filter((s) => s.cliente_id === clienteId);
}

// Posicoes dos clientes ligados em volta da isca: circulo, a partir do topo,
// no sentido horario. So geometria - o que liga e o que pesa vem da API.
export function posicoesNoMapa(n, cx, cy, raio) {
  return Array.from({ length: n }, (_, i) => {
    const angulo = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(n, 1);
    return { x: cx + raio * Math.cos(angulo), y: cy + raio * Math.sin(angulo) };
  });
}

// Escore -> espessura da linha no mapa, entre min e max. O mais forte da caca
// e a referencia: a escala e relativa ao que se esta vendo.
export function espessura(escore, maior, min = 1, max = 6) {
  if (!(maior > 0)) return min;
  return min + (max - min) * Math.min(1, Math.max(0, escore / maior));
}
