// Testes da logica pura da tela (mesa/web/logica.js). Rodam com o test runner
// nativo do Node, sem dependencia nenhuma:  node --test "tests/web/*.test.mjs"
// (o Node 22 nao descobre testes a partir de um diretorio - precisa do glob)
// O pytest os executa tambem (tests/test_mesa_web.py), para a suite ter um
// ponto de entrada so.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  alvoDaFonte, explicacaoDaMarca, formatarBRL, formatarData, formatarMomento,
  operacaoEhAlvo, rotuloDaFonte, segmentar, situacaoDaFila,
  camposDaDecisao, podeDecidir, formatarDuracao, formatarPercentual,
  agruparLigacoes, suspeitasDoCliente, posicoesNoMapa, espessura, ROTULO_CACA,
} from "../../mesa/web/logica.js";

// ---------- segmentar: o texto do parecer nunca pode perder um pedaco ----------

const juntar = (trechos) => trechos.map((t) => t.texto).join("");

test("segmentar marca exatamente as posicoes gravadas", () => {
  const texto = "Media de R$7.395,9 e evento de R$6.913,84 no cartao.";
  const marcas = [
    { inicio: 9, fim: 18, classe: "confirmado", fonte: "media_cliente" },
    { inicio: 31, fim: 41, classe: "atipico_incorreto", fonte: "operacao OP-00269" },
  ];
  const trechos = segmentar(texto, marcas);
  assert.deepEqual(trechos.filter((t) => t.marca).map((t) => t.texto), ["R$7.395,9", "R$6.913,84"]);
  assert.equal(juntar(trechos), texto);
});

test("segmentar sem marcas devolve o texto inteiro", () => {
  assert.deepEqual(segmentar("sem numeros aqui", []), [{ texto: "sem numeros aqui", marca: null }]);
  assert.deepEqual(segmentar("sem numeros aqui", null), [{ texto: "sem numeros aqui", marca: null }]);
});

test("segmentar ordena marcas fora de ordem", () => {
  const texto = "A R$1 B R$2 C";
  const trechos = segmentar(texto, [
    { inicio: 8, fim: 11, classe: "confirmado" },
    { inicio: 2, fim: 5, classe: "confirmado" },
  ]);
  assert.deepEqual(trechos.filter((t) => t.marca).map((t) => t.texto), ["R$1", "R$2"]);
  assert.equal(juntar(trechos), texto);
});

test("marca ruim e ignorada, mas o texto sai inteiro", () => {
  // Perder uma marcacao e aceitavel; perder parte do parecer que o analista
  // precisa ler, nao.
  const texto = "Valor de R$ 100,00 citado.";
  const ruins = [
    { inicio: 9, fim: 18, classe: "confirmado" },   // valida
    { inicio: 12, fim: 20, classe: "confirmado" },  // sobreposta a anterior
    { inicio: 20, fim: 999, classe: "confirmado" }, // passa do fim do texto
    { inicio: 5, fim: 5, classe: "confirmado" },    // vazia
    { inicio: -3, fim: 2, classe: "confirmado" },   // negativa
    { inicio: "9", fim: 18, classe: "confirmado" }, // tipo errado
  ];
  const trechos = segmentar(texto, ruins);
  assert.equal(juntar(trechos), texto);
  assert.equal(trechos.filter((t) => t.marca).length, 1);
});

// ---------- fonte -> alvo: o que o numero clicado destaca ----------

test("alvoDaFonte reconhece os nomes que o verificador grava", () => {
  assert.deepEqual(alvoDaFonte("operacao OP-00269"), { tipo: "operacao", id: "OP-00269" });
  assert.deepEqual(alvoDaFonte("soma_do_dia_2026-05-26"), { tipo: "dia", data: "2026-05-26" });
  assert.deepEqual(alvoDaFonte("soma_canal_ted"), { tipo: "canal", canal: "ted" });
  assert.deepEqual(alvoDaFonte("mediana_cliente"), { tipo: "agregado", nome: "mediana" });
  assert.deepEqual(alvoDaFonte("media_cliente"), { tipo: "agregado", nome: "media" });
  assert.deepEqual(alvoDaFonte("volume_total_cliente"), { tipo: "agregado", nome: "volume" });
  assert.equal(alvoDaFonte(null), null);
  assert.equal(alvoDaFonte("fonte_desconhecida"), null);
});

test("a mediana NAO aponta para operacao nenhuma", () => {
  // O bug que o verificador ja teve: com numero impar de operacoes, a mediana
  // coincide com o valor de uma operacao real. Clicar nela na tela tem que
  // destacar "mediana do cliente", nunca aquela operacao.
  const alvo = alvoDaFonte("mediana_cliente");
  const operacaoDoMeio = { id: "OP-00041", data: "2026-05-07", canal: "pix", valor_brl: 2308.41 };
  assert.equal(operacaoEhAlvo(operacaoDoMeio, alvo), false);
});

test("operacaoEhAlvo liga por id, por dia e por canal", () => {
  const op = { id: "OP-1", data: "2026-05-26", canal: "ted" };
  assert.equal(operacaoEhAlvo(op, { tipo: "operacao", id: "OP-1" }), true);
  assert.equal(operacaoEhAlvo(op, { tipo: "operacao", id: "OP-2" }), false);
  assert.equal(operacaoEhAlvo(op, { tipo: "dia", data: "2026-05-26" }), true);
  assert.equal(operacaoEhAlvo(op, { tipo: "canal", canal: "ted" }), true);
  assert.equal(operacaoEhAlvo(op, { tipo: "canal", canal: "pix" }), false);
  assert.equal(operacaoEhAlvo(op, null), false);
});

test("rotulos das fontes sao legiveis", () => {
  assert.equal(rotuloDaFonte("operacao OP-00269"), "operação OP-00269");
  assert.equal(rotuloDaFonte("soma_do_dia_2026-05-26"), "soma do dia 26/05/2026");
  assert.equal(rotuloDaFonte("soma_canal_ted"), "soma do canal TED");
  assert.equal(rotuloDaFonte("mediana_cliente"), "mediana do cliente");
});

test("explicacao do atipico incorreto diz o que esta errado", () => {
  const texto = explicacaoDaMarca({ classe: "atipico_incorreto", fonte: "operacao OP-00269" });
  assert.match(texto, /OP-00269/);
  assert.match(texto, /NÃO é uma das operações sinalizadas/);
});

// ---------- fila: "nao triado" nao e "diverge" ----------

test("situacaoDaFila distingue nao triado de diverge", () => {
  assert.equal(situacaoDaFila({ concorda: null }), "nao_triado");
  assert.equal(situacaoDaFila({ concorda: undefined }), "nao_triado");
  assert.equal(situacaoDaFila({ concorda: false }), "diverge");
  assert.equal(situacaoDaFila({ concorda: true }), "concorda");
});

// ---------- formatacao ----------

test("formatarBRL usa o padrao brasileiro", () => {
  // Intl pode usar espaco nao separavel entre "R$" e o numero; normaliza
  assert.equal(formatarBRL(6913.84).replace(/\s/g, " "), "R$ 6.913,84");
  assert.equal(formatarBRL(null), "—");
});

test("formatarData nao desloca o dia pelo fuso", () => {
  // new Date("2026-05-26") seria meia-noite UTC = dia 25 no horario de Brasilia
  assert.equal(formatarData("2026-05-26"), "26/05/2026");
  assert.equal(formatarData(null), "sem data");
});

test("formatarMomento deixa o fuso explicito", () => {
  assert.equal(formatarMomento("2026-09-12T17:00:49Z"), "12/09/2026 17:00 UTC");
});

// ---------- Fase 4: decisao ----------

test("campos da decisao seguem as regras da API", () => {
  assert.deepEqual(camposDaDecisao("concordo"), { nivel: "oculto", motivo: "opcional" });
  assert.deepEqual(camposDaDecisao("discordo"), { nivel: "obrigatorio", motivo: "obrigatorio" });
  assert.deepEqual(camposDaDecisao("escalar"), { nivel: "opcional", motivo: "obrigatorio" });
});

test("so o dono de um caso em analise ve o formulario", () => {
  const caso = { estado: "em_analise", analista_id: "ana" };
  assert.equal(podeDecidir(caso, "ana"), true);
  assert.equal(podeDecidir(caso, "  ana "), true);        // a API tambem faz trim
  assert.equal(podeDecidir(caso, "bruno"), false);
  assert.equal(podeDecidir(caso, ""), false);
  assert.equal(podeDecidir({ estado: "triado", analista_id: null }, ""), false); // null != ""
  assert.equal(podeDecidir({ estado: "concluido", analista_id: "ana" }, "ana"), false);
  // Fase 5.1: substituido por dado novo -> decide-se no caso atual
  assert.equal(podeDecidir({ ...caso, substituido_por: 42 }, "ana"), false);
  assert.equal(podeDecidir({ ...caso, substituido_por: null }, "ana"), true);
});

test("duracao legivel", () => {
  assert.equal(formatarDuracao(null), "—");
  assert.equal(formatarDuracao(40), "40 s");
  assert.equal(formatarDuracao(720), "12 min");
  assert.equal(formatarDuracao(3900), "1 h 05 min");
});

test("percentual sem dado e traco, nunca 0%", () => {
  assert.equal(formatarPercentual(null), "—");
  assert.equal(formatarPercentual(0), "0,0%");
  assert.equal(formatarPercentual(23 / 30), "76,7%");
});

// ---------- Fase 6: contra-isca ----------

const lig = (op, cliente, escore, padroes, alerta = 40) =>
  ({ operacao_id: op, cliente_id: cliente, escore, padroes, alerta_do_cliente: alerta });

test("agruparLigacoes junta por cliente e ordena pela ligacao mais forte", () => {
  const grupos = agruparLigacoes([
    lig("OP-1", "CLI-200", 1.0, ["B"]),
    lig("OP-2", "CLI-111", 2.5, ["A"]),
    lig("OP-3", "CLI-200", 3.0, ["A"]),
    lig("OP-4", "CLI-150", 2.5, ["A"]),
  ]);
  assert.deepEqual(grupos.map((g) => g.cliente_id), ["CLI-200", "CLI-111", "CLI-150"]);
  assert.equal(grupos[0].escore, 3.0);
  assert.deepEqual(grupos[0].padroes, ["A", "B"]);
  assert.deepEqual(grupos[0].ligacoes.map((l) => l.operacao_id), ["OP-1", "OP-3"]);
});

test("agruparLigacoes sem ligacao devolve lista vazia", () => {
  assert.deepEqual(agruparLigacoes([]), []);
});

test("suspeitasDoCliente filtra pelo cliente apontado", () => {
  const s = [{ cliente_id: "CLI-111", suspeita_id: 1 }, { cliente_id: "CLI-112", suspeita_id: 2 }];
  assert.deepEqual(suspeitasDoCliente(s, "CLI-112").map((x) => x.suspeita_id), [2]);
  assert.deepEqual(suspeitasDoCliente(undefined, "CLI-112"), []);
});

test("posicoesNoMapa comeca no topo e da a volta no circulo", () => {
  const p = posicoesNoMapa(4, 100, 100, 50);
  assert.equal(p.length, 4);
  assert.ok(Math.abs(p[0].x - 100) < 1e-9 && Math.abs(p[0].y - 50) < 1e-9);   // topo
  assert.ok(Math.abs(p[1].x - 150) < 1e-9);                                    // direita
  assert.deepEqual(posicoesNoMapa(0, 0, 0, 1), []);
});

test("espessura e relativa a ligacao mais forte e fica na faixa", () => {
  assert.equal(espessura(3, 3), 6);
  assert.equal(espessura(0, 3), 1);
  assert.equal(espessura(1.5, 3), 3.5);
  assert.equal(espessura(1, 0), 1);
});

test("o botao nao se chama falso positivo", () => {
  assert.ok(!/falso/i.test(ROTULO_CACA));
});
