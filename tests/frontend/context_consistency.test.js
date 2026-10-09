/* Phase UI — frontend context-consistency behavioural harness.
 *
 * Runs the REAL src/smc_engine/web/static/app.js state machine under a stubbed
 * DOM/transport (no browser, no network). Exercises: chart/quote/label agreement,
 * stale-response rejection, failure clearing the live price, distinct chart
 * states, and the opportunity-vs-chart context separation.
 *
 * Invoked from tests/test_ui_context_consistency.py via `node`. Exits non-zero
 * on the first failed assertion.
 */
"use strict";
const path = require("path");

/* ── minimal DOM stub ─────────────────────────────────────────────────────── */
const els = new Map();
function classList() {
  return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
}
const FILLTEXT = [];
function makeCtx() {
  const rec = { fillText: (t) => FILLTEXT.push(String(t)), _noop: () => {} };
  return new Proxy(rec, {
    get: (t, k) => (k in t ? t[k] : () => {}),
    set: (t, k, v) => { t[k] = v; return true; },
  });
}
function makeEl(name) {
  const el = {
    _name: name, textContent: "", innerHTML: "", className: "",
    classList: classList(), dataset: {}, style: {}, value: "", options: [],
    width: 0, height: 0, clientWidth: 800, clientHeight: 300,
    setAttribute() {}, appendChild() {}, addEventListener() {},
    querySelector() { return makeEl("q"); }, querySelectorAll() { return []; },
    getContext() { return makeCtx(); },
    getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 300 }; },
  };
  return el;
}
function q(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}
const $ = q;
global.document = {
  hidden: false, body: makeEl("body"),
  querySelector: q, querySelectorAll: () => [], addEventListener() {},
  createElement: () => makeEl("created"),
};
global.window = { addEventListener() {} };
global.devicePixelRatio = 1;
let ES = null;   // captured EventSource instance (handlers exercised by tests)
global.EventSource = function () {
  ES = { handlers: {}, addEventListener(k, fn) { this.handlers[k] = fn; }, close() {} };
  return ES;
};
global.localStorage = { _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = String(v); } };
global.setInterval = () => 0;
global.setTimeout = (fn) => { try { fn(); } catch (e) {} return 0; };
global.alert = () => {};
globalThis.__SMC_TEST__ = true;

/* ── controllable transport ───────────────────────────────────────────────── */
let ROUTES = {};                 // path (no query) → body | fn(path,opts)
function setRoute(p, handler) { ROUTES[p.split("?")[0]] = handler; }
global.fetch = async (p) => {
  const key = String(p).split("?")[0];
  const h = ROUTES[key];
  if (!h) return { ok: false, status: 404, statusText: "Not Found", json: async () => ({ detail: "no route " + key }) };
  const res = typeof h === "function" ? await h(p) : h;
  return {
    ok: res.ok !== false, status: res.status || 200, statusText: res.statusText || "OK",
    json: async () => res.body,
  };
};

/* ── load the real app ────────────────────────────────────────────────────── */
const APP = path.join(__dirname, "..", "..", "src", "smc_engine", "web", "static", "app.js");
const app = require(APP);
const { S } = app;

const T = (t, o = 1.6, c = 1.6005) => [t, o, c, o - 0.001, c];
function analysis(sym, tf, last) {
  return {
    symbol: sym, timeframe: tf, last_closed_close: last,
    last_closed_candle_time: "2026-10-09T00:00:00Z",
    quote: { market_data: "AVAILABLE", bid: last, ask: last + 0.0002, spread: 2e-4,
             tick_time: "2026-10-09T00:00:01Z", source: "mt5" },
    candles: [T("2026-10-08T23:45:00Z", last - 0.01, last), T("2026-10-09T00:00:00Z", last - 0.005, last)],
    candidates: [], pois: [], fvgs: [], order_blocks: [], swings: [], sweeps: [], structure_events: [],
    identity: null,
  };
}
let failures = 0;
function ok(cond, msg) { if (cond) { console.log("  ok  - " + msg); } else { failures++; console.log("  FAIL- " + msg); } }

(async () => {
  S.status = { paper_enabled: false, live_execution_enabled: false, account: null,
               account_mode: "DEMO", mt5: "CONNECTED", market_data: "OK" };

  /* 1. symbol → chart/quote/label/price all agree (XAUUSD). */
  console.log("[1] XAUUSD selection coherence");
  S.symbol = "EURAUD"; S.tf = "M15";
  setRoute("/api/analysis/XAUUSD/M15", { body: analysis("XAUUSD", "M15", 4168.998) });
  setRoute("/api/risk", { body: { status: "RISK_OK" } });
  setRoute("/api/setups", { body: { setups: [] } });
  app.selectChartSymbol("XAUUSD");
  await app.loadAnalysis();
  ok($("#chart-symbol").textContent === "XAUUSD", "chart header == XAUUSD");
  ok($("#chart-tf").textContent.includes("M15"), "chart tf == M15");
  ok(($("#chart-price").textContent || "").startsWith("4168"), "price tag == XAUUSD price");
  ok(($("#quote-strip").innerHTML || "").includes("XAUUSD"), "quote strip names XAUUSD");
  ok(S.analysis && S.analysis.symbol === "XAUUSD", "S.analysis is XAUUSD");
  ok(S.chartState === "ready", "chartState ready");

  /* 2. chart is labelled from DRAWN DATA, never the (newer) selection. */
  console.log("[2] chart label derives from drawn data");
  S.symbol = "XAUUSD"; S.analysis = analysis("EURAUD", "M15", 1.6);
  $("#chart-symbol").textContent = "";
  app.drawChart();
  ok($("#chart-symbol").textContent === "EURAUD", "header shows the drawn data symbol (EURAUD), not selection");

  /* 3. switching symbol must not be overwritten by a delayed old response. */
  console.log("[3] stale response rejected");
  let resolveEu;
  setRoute("/api/analysis/EURAUD/M15", () => new Promise(r => { resolveEu = () => r({ body: analysis("EURAUD", "M15", 1.6) }); }));
  setRoute("/api/analysis/XAUUSD/M15", { body: analysis("XAUUSD", "M15", 4168.998) });
  S.symbol = "EURAUD"; S.symbolSeq = 0;
  const pEu = app.loadAnalysis();            // old, in flight
  app.selectChartSymbol("XAUUSD");           // user switches
  await app.loadAnalysis();                  // new completes first
  resolveEu();                               // old resolves late
  await pEu;
  ok(S.analysis && S.analysis.symbol === "XAUUSD", "late EURAUD response discarded; analysis stays XAUUSD");
  ok($("#chart-symbol").textContent === "XAUUSD", "chart header stays XAUUSD");

  /* 4. a failed analysis clears the stale live price and shows unavailable. */
  console.log("[4] failure clears stale price");
  S.symbol = "XAUUSD"; S.analysis = analysis("XAUUSD", "M15", 4168.998);
  app.updatePriceTag(4168.998);
  setRoute("/api/analysis/EURAUD/M15", { ok: false, status: 503, body: { detail: "market data unavailable" } });
  app.selectChartSymbol("EURAUD");
  await app.loadAnalysis();
  ok($("#chart-price").textContent === "—", "price tag cleared on failure (no stale live quote)");
  ok(($("#quote-strip").innerHTML || "").includes("DATA UNAVAILABLE"), "quote shows DATA UNAVAILABLE");
  ok(S.analysis === null && S.chartState === "unavailable", "state unavailable");

  /* 5. distinct load / unavailable / empty states render distinctly. */
  console.log("[5] distinct chart states");
  FILLTEXT.length = 0; S.analysis = null; S.symbol = "XAUUSD"; S.tf = "M15";
  S.chartState = "loading"; app.drawChart();
  const loadingMsg = FILLTEXT.join("|");
  FILLTEXT.length = 0; S.chartState = "unavailable"; app.drawChart();
  const unavailMsg = FILLTEXT.join("|");
  FILLTEXT.length = 0; S.chartState = "empty"; S.analysis = analysis("XAUUSD", "M15", 1); S.analysis.candles = [];
  app.drawChart();
  const emptyMsg = FILLTEXT.join("|");
  ok(loadingMsg !== unavailMsg && unavailMsg !== emptyMsg && loadingMsg !== emptyMsg, "three states render distinct text");

  /* 6. selecting a READY opportunity shows ITS identity + lifecycle. */
  console.log("[6] opportunity context");
  setRoute("/api/opportunities/OPP-XAU-1", { body: { opportunity: { opportunity_id: "OPP-XAU-1" },
      history: [{ at: "2026-10-08T20:00:00Z", to: "READY_FOR_MITIGATION", reason: "READY_FOR_MITIGATION" },
                { at: "2026-10-08T20:05:00Z", to: "ENTRY_TRIGGERED", reason: "ENTRY_PASSED" }] } });
  setRoute("/api/analysis/XAUUSD/M15", { body: analysis("XAUUSD", "M15", 4168.998) });
  app.selectOpportunity({ opportunity_id: "OPP-XAU-1", symbol: "XAUUSD", direction: "BULLISH",
    type: "REVERSAL", state: "READY_FOR_MITIGATION", label: "READY", created_at: "2026-10-08T19:00:00Z",
    updated_at: "2026-10-08T20:05:00Z", risk_status: "", setup_id: "" });
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  const sb = $("#setup-body").innerHTML || "";
  ok(sb.includes("OPP-XAU-1"), "setup pane shows the opportunity identity");
  ok(sb.includes("READY"), "setup pane shows the lifecycle label READY");
  ok(!/SMC EXECUTION READY|EXECUTION_READY/.test(sb) || sb.includes("NOT an executable setup"),
     "opportunity is not presented as an executable/execution-ready setup");
  ok(($("#ctx-context").innerHTML || "").includes("OPPORTUNITY CONTEXT"), "context banner = OPPORTUNITY CONTEXT");
  ok(S.selectedOpp && S.selectedOpp.opportunity_id === "OPP-XAU-1", "S.selectedOpp set");

  /* 7. selecting a DIFFERENT opportunity replaces the previous one. */
  console.log("[7] opportunity switch");
  setRoute("/api/opportunities/OPP-ETH-2", { body: { opportunity: { opportunity_id: "OPP-ETH-2" }, history: [] } });
  app.selectOpportunity({ opportunity_id: "OPP-ETH-2", symbol: "ETHUSD", direction: "BEARISH",
    type: "REVERSAL", state: "READY_FOR_MITIGATION", label: "READY", created_at: "x", updated_at: "y" });
  await new Promise(r => setTimeout(r, 0));
  const sb2 = $("#setup-body").innerHTML || "";
  ok(sb2.includes("OPP-ETH-2") && !sb2.includes("OPP-XAU-1"), "second opportunity replaces the first (no stale identity)");

  /* 8. clearing selection returns to chart context with NO CAUSAL SETUP. */
  console.log("[8] chart context + no causal setup");
  S.selectedOpp = null; S.analysis = analysis("XAUUSD", "M15", 4168.998);   // candidates: []
  app.renderSetup();
  const sb3 = $("#setup-body").innerHTML || "";
  ok(sb3.includes("NO CAUSAL SETUP DETECTED"), "chart context shows NO CAUSAL SETUP DETECTED");
  ok(sb3.includes("chart context"), "chart context is labelled");
  ok(($("#ctx-context").innerHTML || "").includes("CHART CONTEXT"), "banner = CHART CONTEXT");

  /* 9. timeframe change while the previous timeframe's request is outstanding. */
  console.log("[9] timeframe change invalidates outstanding request");
  let resolveM15;
  setRoute("/api/analysis/XAUUSD/M15", () => new Promise(r => { resolveM15 = () => r({ body: analysis("XAUUSD", "M15", 1.6) }); }));
  setRoute("/api/analysis/XAUUSD/H1", { body: analysis("XAUUSD", "H1", 4168.998) });
  S.symbol = "XAUUSD"; S.tf = "M15"; S.symbolSeq = 0; S.analysis = null;
  const p15 = app.loadAnalysis();
  app.selectTimeframe("H1");                 // user switches timeframe
  await app.loadAnalysis();
  resolveM15();                              // old timeframe resolves late
  await p15;
  ok(S.analysis && S.analysis.timeframe === "H1", "late M15 response discarded after timeframe switch");
  ok($("#chart-tf").textContent.includes("H1"), "chart timeframe shows H1");

  /* 10. rapid multi-symbol switching with out-of-order resolution. */
  console.log("[10] rapid switching, out-of-order resolution");
  const def = {};
  for (const s of ["EURUSD", "GBPUSD", "XAUUSD"]) {
    setRoute(`/api/analysis/${s}/H1`, () => new Promise(r => { def[s] = () => r({ body: analysis(s, "H1", s === "XAUUSD" ? 4168.998 : 1.6) }); }));
  }
  S.symbol = "EURUSD"; S.tf = "H1"; S.symbolSeq = 0; S.analysis = null;
  const pa = app.loadAnalysis();             // EURUSD outstanding
  app.selectChartSymbol("GBPUSD");           // switch 1 (fires GBPUSD)
  app.selectChartSymbol("XAUUSD");           // switch 2 (fires XAUUSD)
  const pc = app.loadAnalysis();             // current (XAUUSD) outstanding
  def["XAUUSD"](); await pc;                 // current resolves first
  ok(S.analysis && S.analysis.symbol === "XAUUSD", "current symbol response applied");
  def["GBPUSD"](); def["EURUSD"]();          // stale responses arrive late
  await new Promise(r => setTimeout(r, 0));
  ok(S.analysis && S.analysis.symbol === "XAUUSD", "late stale responses do not overwrite current");
  ok($("#chart-symbol").textContent === "XAUUSD", "chart header pinned to current symbol");

  /* 11. an SSE tick after a context change must not restore the old symbol. */
  console.log("[11] SSE tick cannot restore an obsolete context");
  app.openStream();                          // register real SSE handlers
  S.symbol = "XAUUSD"; S.symbolSeq = 100; S.analysis = analysis("XAUUSD", "M15", 4168.998);
  app.updatePriceTag(4000);
  const beforeTick = $("#chart-price").textContent;
  ES.handlers["MARKET_UPDATE"]({ data: JSON.stringify({ payload: { symbol: "EURAUD", price: 1.6 } }) });
  ok($("#chart-price").textContent === beforeTick, "tick for a non-selected symbol is ignored");
  ES.handlers["MARKET_UPDATE"]({ data: JSON.stringify({ payload: { symbol: "XAUUSD", price: 4170 } }) });
  ok(($("#chart-price").textContent || "").startsWith("4170"), "tick for the current symbol is applied");

  /* 12. a failed request cleans up loading + price, not stuck loading. */
  console.log("[12] loading/error cleanup");
  setRoute("/api/analysis/GBPUSD/M15", { ok: false, status: 503, body: { detail: "down" } });
  S.symbol = "GBPUSD"; S.tf = "M15"; S.symbolSeq = 0; S.analysis = analysis("GBPUSD", "M15", 1.3);
  app.updatePriceTag(1.3);
  await app.loadAnalysis();
  ok(S.chartState === "unavailable", "chart state = unavailable after failure");
  ok($("#chart-price").textContent === "—", "price cleared, not stuck showing a stale value");
  ok(($("#quote-strip").innerHTML || "").includes("DATA UNAVAILABLE"), "quote shows DATA UNAVAILABLE");

  console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILED`);
  process.exit(failures === 0 ? 0 : 1);
})().catch(e => { console.error("HARNESS ERROR:", e && e.stack || e); process.exit(2); });
