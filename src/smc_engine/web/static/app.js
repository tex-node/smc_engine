/* SMC ENGINE workstation frontend.
   View + user intent ONLY. All analytics/risk/execution come from the backend. */
"use strict";

const TF_ORDER = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const S = {
  symbol: null, tf: "M15", analysis: null, setups: [], selected: null,
  mode: "ANALYSIS", status: {}, symbols: [], prices: {}, layers: {},
};
const $ = (q) => document.querySelector(q);
const $$ = (q) => document.querySelectorAll(q);
const fmt = (v, d = 5) => v == null ? "—" : Number(v).toFixed(d);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText; try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(`${r.status}: ${msg}`);
  }
  return r.json();
}
function toast(msg, cls = "") {
  const t = document.createElement("div");
  t.className = "toast " + cls; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 4200);
}

/* ---------------- boot ---------------- */
async function boot() {
  S.status = await api("/api/status").catch(() => ({ mt5: "DISCONNECTED" }));
  renderStatus();
  try { S.symbols = (await api("/api/symbols")).symbols; } catch (e) { S.symbols = []; }
  S.symbol = S.symbols.includes("EURAUD") ? "EURAUD" : (S.symbols[0] || "EURAUD");
  buildTfButtons(); buildSymbolList(); wire();
  openStream();
  await refresh();
}

function wire() {
  $$("#tf-buttons button").forEach(b => b.onclick = () => { S.tf = b.dataset.tf; buildTfButtons(); refresh(); });
  $$("#overlay-toggles input").forEach(c => c.onchange = () => { S.layers[c.dataset.layer] = c.checked; drawChart(); });
  $$('input[name=mode]').forEach(r => r.onchange = () => { S.mode = r.value; renderExec(); renderSetup(); });
  $("#symbol-search").oninput = (e) => buildSymbolList(e.target.value);
  $("#p-tf").innerHTML = TF_ORDER.map(t => `<option${t === "H1" ? " selected" : ""}>${t}</option>`).join("");
  $("#p-symbol").value = S.symbol;
  $("#plan-new").onclick = () => $("#plan-form").classList.toggle("hidden");
  $("#p-save").onclick = savePlan;
  ["f-status", "f-dir", "f-symbol"].forEach(id => $("#" + id).oninput = renderHistory);
  setInterval(() => { $("#clock").textContent = new Date().toISOString().slice(11, 19) + "Z"; }, 1000);
  setInterval(() => { if (!document.hidden) loadAnalysis(true); }, 20000);
}

/* ---------------- data loads ---------------- */
async function refresh() { await Promise.all([loadAnalysis(), loadSetups(), loadAlerts(), loadHypotheses(), loadHistory()]); }

async function loadAnalysis(quiet) {
  try {
    S.analysis = await api(`/api/analysis/${S.symbol}/${S.tf}?count=250`);
    S.prices[S.symbol] = S.analysis.last_closed_close;
    drawChart(); renderQuote(); renderSetup(); renderRisk(); renderLifecycle();
  } catch (e) {
    S.analysis = null;
    renderQuote(null, e.message);
    renderSetup(); renderLifecycle(); drawChart();
  }
}

function renderQuote(a, err) {
  const el = $("#quote-strip");
  if (!el) return;
  if (!a) {
    el.innerHTML = `<span class="q down">MARKET DATA UNAVAILABLE${err ? " — " + esc(err) : ""}</span>`;
    return;
  }
  const q = a.quote || { market_data: "UNAVAILABLE" };
  if (q.market_data !== "AVAILABLE") {
    el.innerHTML = `<span class="q">SYMBOL ${esc(a.symbol)} · TF ${esc(a.timeframe)} · </span>` +
      `<span class="q down">BID/ASK: MARKET DATA UNAVAILABLE</span>`;
    return;
  }
  el.innerHTML =
    `<span class="q">BID <b>${fmt(q.bid)}</b></span>` +
    `<span class="q">ASK <b>${fmt(q.ask)}</b></span>` +
    `<span class="q">SPREAD <b>${fmt(q.spread)}</b></span>` +
    `<span class="q">LAST UPDATE <b>${esc(q.tick_time || a.last_closed_candle_time)}</b></span>` +
    `<span class="q">SOURCE <b>${esc(q.source)}</b></span>` +
    `<span class="q">STATE <b class="v bull">CONNECTED</b></span>`;
}
async function loadSetups() {
  try {
    const url = `/api/setups?symbol=${encodeURIComponent(S.symbol)}`;
    S.setups = (await api(url)).setups || [];
  } catch (e) { S.setups = []; }
  if (S.selected && !S.setups.find(s => s.setup_id === S.selected)) S.selected = null;
  renderLifecycle(); renderSetup();
}
async function loadAlerts() { try { renderAlerts((await api("/api/alerts?limit=50")).alerts); } catch (e) {} }
async function loadHypotheses() { try { renderObserver((await api("/api/hypotheses")).hypotheses); } catch (e) {} }
async function loadHistory() { try { renderHistory((await api("/api/history")).rows); } catch (e) {} }

/* ---------------- SSE ---------------- */
function openStream() {
  const es = new EventSource("/api/events");
  es.addEventListener("MARKET_UPDATE", (ev) => {
    const p = JSON.parse(ev.data).payload;
    if (p.symbol === S.symbol && p.price) { S.prices[p.symbol] = p.price; updatePriceTag(p.price); }
  });
  ["SETUP_CREATED", "SETUP_UPDATED", "SETUP_INVALIDATED"].forEach(k =>
    es.addEventListener(k, () => { loadSetups(); if (k !== "SETUP_UPDATED") { loadAnalysis(true); } }));
  ["ORDER_PREPARED", "ORDER_PLACED", "ORDER_CANCELLED"].forEach(k =>
    es.addEventListener(k, (ev) => { loadSetups(); loadHistory();
      toast(`${k}: ${JSON.parse(ev.data).payload.setup_id || ""}`); }));
  es.addEventListener("ALERT_CREATED", () => loadAlerts());
  es.onerror = () => { $("#conn-badge").textContent = "STREAM DOWN"; $("#conn-badge").className = "badge err"; };
  es.onopen = () => renderStatus();
}

/* ---------------- status ---------------- */
function renderStatus() {
  const st = S.status;
  $("#s-engine").textContent = st.engine || "–";
  $("#s-mt5").textContent = st.mt5 || "–";
  $("#s-acct").textContent = st.account_mode || "–";
  $("#s-md").textContent = st.market_data || "–";
  $("#s-risk").textContent = st.risk_engine || "–";
  $("#s-exec").textContent = st.live_execution_enabled ? "LIVE-ON" : (st.execution || "–");
  const badge = $("#account-badge");
  badge.textContent = st.account ? `${st.account_mode} · ${st.account.server}` : "NO ACCOUNT";
  badge.className = "badge " + (st.account_mode === "DEMO" ? "demo" : st.account_mode === "LIVE" ? "live" : "");
  $("#conn-badge").textContent = st.mt5 === "CONNECTED" ? "CONNECTED" : "DISCONNECTED";
  $("#conn-badge").className = "badge " + (st.mt5 === "CONNECTED" ? "demo" : "err");
}
function updatePriceTag(p) {
  const el = $("#chart-price");
  const prev = parseFloat(el.dataset.v || p);
  el.textContent = fmt(p); el.dataset.v = p;
  el.className = "price " + (p >= prev ? "up" : "down");
}

/* ---------------- symbol rail ---------------- */
function buildSymbolList(filter = "") {
  const box = $("#symbol-list"); box.innerHTML = "";
  const f = filter.toUpperCase();
  const majors = ["EURUSD","GBPUSD","USDJPY","XAUUSD","XAGUSD","EURAUD"];
  const list = S.symbols.filter(s => s.includes(f) && !/^[A-Z]{2}\d/.test(s));
  const ordered = [...majors.filter(m => list.includes(m)), ...list.filter(s => !majors.includes(s))];
  for (const sym of ordered.slice(0, 300)) {
    const d = document.createElement("div");
    d.className = "sym" + (sym === S.symbol ? " sel" : "");
    d.innerHTML = `<span>${esc(sym)}</span><span class="last">${S.prices[sym] ? fmt(S.prices[sym], 4) : ""}</span>`;
    d.onclick = () => { S.symbol = sym; $("#p-symbol").value = sym; buildSymbolList(filter); refresh(); };
    box.appendChild(d);
  }
}
function buildTfButtons() {
  $("#tf-buttons").innerHTML = TF_ORDER.map(t =>
    `<button data-tf="${t}" class="${t === S.tf ? "sel" : ""}">${t}</button>`).join("");
}

/* ---------------- chart ---------------- */
const cv = $("#chart"), cx = cv.getContext("2d");
function L(name) { return S.layers[name] !== false; }

function drawChart() {
  const a = S.analysis;
  const W = cv.clientWidth, H = cv.clientHeight, DPR = devicePixelRatio || 1;
  cv.width = W * DPR; cv.height = H * DPR; cx.setTransform(DPR, 0, 0, DPR, 0, 0);
  cx.clearRect(0, 0, W, H);
  $("#chart-symbol").textContent = S.symbol + (a ? "" : " — no data");
  $("#chart-tf").textContent = a ? "· " + a.timeframe : "";
  if (!a || !a.candles || !a.candles.length) {
    cx.fillStyle = "#46506a"; cx.font = "13px monospace";
    cx.fillText(a ? "not enough candles" : "MT5 DISCONNECTED — analysis unavailable", 20, 30);
    return;
  }
  updatePriceTag(a.last_closed_close);
  const vis = Math.min(150, a.candles.length);
  const candles = a.candles.slice(-vis);
  const padR = 62, padL = 8, padT = 10, padB = 22;
  const cw = (W - padR - padL) / vis;
  const timeIdx = new Map(a.candles.map((c, i) => [c[0], i]));
  const off = a.candles.length - vis;
  let hi = -Infinity, lo = Infinity;
  for (const c of candles) { hi = Math.max(hi, c[2]); lo = Math.min(lo, c[3]); }
  const setup = (setupModel().cand);
  if (setup) { hi = Math.max(hi, setup.take_profit, setup.entry); lo = Math.min(lo, setup.stop_loss, setup.entry); }
  const rng = hi - lo || 1; hi += rng * 0.06; lo -= rng * 0.06;
  const X = (i) => padL + (i - off + 0.5) * cw;
  const Y = (p) => padT + (hi - p) / (hi - lo) * (H - padT - padB);

  // grid + axes
  cx.strokeStyle = "#1f2636"; cx.fillStyle = "#6b7794"; cx.font = "10px monospace"; cx.lineWidth = 1;
  for (let g = 0; g <= 5; g++) {
    const p = hi - (hi - lo) * g / 5, y = Y(p);
    cx.beginPath(); cx.moveTo(padL, y); cx.lineTo(W - padR, y); cx.stroke();
    cx.fillText(fmt(p), W - padR + 6, y + 3);
  }
  const step = Math.max(1, Math.floor(vis / 6));
  for (let i = 0; i < vis; i += step) {
    cx.fillText(String(candles[i][0]).slice(5, 16).replace("T", " "), X(off + i) - 24, H - 8);
  }

  // FVG zones
  if (L("fvgs")) for (const g of a.fvgs) {
    const i0 = timeIdx.get(g.time); if (i0 == null) continue;
    const col = g.direction === "BULLISH" ? "rgba(45,212,167," : "rgba(255,93,115,";
    cx.fillStyle = col + (g.state === "MITIGATED" ? "0.04)" : "0.11)");
    cx.fillRect(X(i0), Y(g.top), Math.max(6, W - padR - X(i0)), Y(g.bottom) - Y(g.top));
  }
  // POIs
  if (L("pois")) for (const p of a.pois) {
    const i0 = nearestIdx(p); if (i0 == null) continue;
    cx.fillStyle = p.direction === "BULLISH" ? "rgba(45,212,167,.07)" : "rgba(255,93,115,.07)";
    cx.fillRect(X(i0), Y(p.high), W - padR - X(i0), Y(p.low) - Y(p.high));
  }
  // OB zones
  if (L("ob")) for (const b of a.order_blocks) {
    const i0 = timeIdx.get(b.time); if (i0 == null) continue;
    cx.strokeStyle = b.direction === "BULLISH" ? "rgba(45,212,167,.55)" : "rgba(255,93,115,.55)";
    cx.setLineDash([2, 3]); cx.strokeRect(X(i0), Y(b.high), Math.max(8, (off + vis - i0) * cw * 0.6), Y(b.low) - Y(b.high));
    cx.setLineDash([]);
  }
  // candles
  for (let i = 0; i < vis; i++) {
    const [, o, h, l, c] = candles[i]; const x = X(off + i);
    cx.strokeStyle = cx.fillStyle = c >= o ? "#26a69a" : "#ef5350";
    cx.beginPath(); cx.moveTo(x, Y(h)); cx.lineTo(x, Y(l)); cx.stroke();
    const bw = Math.max(1.5, cw * 0.62);
    cx.fillRect(x - bw / 2, Math.min(Y(o), Y(c)), bw, Math.max(1, Math.abs(Y(c) - Y(o))));
  }
  // swings
  if (L("swings")) for (const s of a.swings) {
    if (s.index < off) continue;
    const x = X(s.index), y = Y(s.price);
    cx.fillStyle = s.type === "HIGH" ? "#ef5350" : "#26a69a";
    cx.beginPath(); cx.arc(x, s.type === "HIGH" ? y - 6 : y + 6, 2.4, 0, 7); cx.fill();
  }
  // sweeps
  if (L("sweeps")) for (const w of a.sweeps) {
    const i0 = timeIdx.get(w.time); if (i0 == null || i0 < off) continue;
    const x = X(i0), y = Y(w.sweep_extreme);
    cx.fillStyle = w.side === "SELL_SIDE" ? "#2dd4a7" : "#ff5d73";
    cx.beginPath(); cx.arc(x, y, 4, 0, 7); cx.fill();
    cx.strokeStyle = cx.fillStyle; cx.setLineDash([2, 2]);
    cx.beginPath(); cx.moveTo(padL, Y(w.swept_level)); cx.lineTo(x, Y(w.swept_level)); cx.stroke(); cx.setLineDash([]);
  }
  // BOS / CHOCH
  if (L("events")) for (const e of a.structure_events) {
    const i0 = timeIdx.get(e.time); if (i0 == null || i0 < off) continue;
    const y = Y(e.level), bull = e.direction === "BULLISH";
    cx.strokeStyle = bull ? "#2dd4a7" : "#ff5d73"; cx.setLineDash([5, 3]);
    cx.beginPath(); cx.moveTo(X(i0), y); cx.lineTo(W - padR, y); cx.stroke(); cx.setLineDash([]);
    cx.fillStyle = bull ? "#2dd4a7" : "#ff5d73";
    cx.font = "9.5px monospace";
    cx.fillText(`${e.type}${bull ? "↑" : "↓"} ${e.level.toFixed(4)}`, X(i0) + 2, y - 3);
  }
  // setup levels
  if (setup && L("setup")) {
    const bull = setup.direction === "BULLISH";
    hline(setup.entry, "#4f8cff", `ENTRY ${fmt(setup.entry)}`);
    hline(setup.stop_loss, "#ef5350", `STOP ${fmt(setup.stop_loss)}`);
    hline(setup.take_profit, "#26a69a", `TARGET ${fmt(setup.take_profit)}`);
    cx.fillStyle = bull ? "rgba(79,140,255,.07)" : "rgba(79,140,255,.07)";
    cx.fillRect(padL, Math.min(Y(setup.take_profit), Y(setup.entry)), W - padR - padL, Math.abs(Y(setup.take_profit) - Y(setup.entry)));
    cx.fillStyle = "rgba(239,83,80,.08)";
    cx.fillRect(padL, Math.min(Y(setup.stop_loss), Y(setup.entry)), W - padR - padL, Math.abs(Y(setup.stop_loss) - Y(setup.entry)));
  }
  function hline(p, col, label) {
    if (p == null) return; const y = Y(p);
    cx.strokeStyle = col; cx.lineWidth = 1.4;
    cx.beginPath(); cx.moveTo(padL, y); cx.lineTo(W - padR, y); cx.stroke(); cx.lineWidth = 1;
    cx.fillStyle = col; cx.font = "10px monospace"; cx.fillText(label, padL + 4, y - 4);
  }
  function nearestIdx(rec) {
    if (rec.time == null) return null;
    let best = null, bd = Infinity;
    for (let i = off; i < a.candles.length; i++) {
      const d = Math.abs(new Date(a.candles[i][0]) - new Date(rec.time));
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }
}

/* crosshair */
cv.addEventListener("mousemove", (ev) => {
  const a = S.analysis; const tip = $("#tooltip");
  if (!a || !a.candles.length) return;
  const r = cv.getBoundingClientRect();
  const vis = Math.min(150, a.candles.length), padL = 8, padR = 62;
  const i = Math.floor((ev.clientX - r.left - padL) / ((r.width - padR - padL) / vis));
  const c = a.candles.slice(-vis)[i];
  if (!c) { tip.classList.add("hidden"); return; }
  tip.classList.remove("hidden");
  tip.style.left = Math.min(r.width - 200, ev.clientX - r.left + 14) + "px";
  tip.style.top = (ev.clientY - r.top + 12) + "px";
  tip.innerHTML = `${esc(String(c[0]).slice(0, 16))}<br>O ${fmt(c[1])} H ${fmt(c[2])}<br>L ${fmt(c[3])} C ${fmt(c[4])}`;
});
cv.addEventListener("mouseleave", () => $("#tooltip").classList.add("hidden"));
window.addEventListener("resize", () => drawChart());

/* ---------------- panels ---------------- */
function setupModel() {
  const cands = (S.analysis && S.analysis.candidates) || [];
  const c = cands.find(x => x.id === S.selected) || cands[0] || null;
  const row = c ? S.setups.find(s => s.setup_id === c.id) : null;
  return { cand: c, row };
}

function renderSetup() {
  const box = $("#setup-body");
  const { cand: c, row } = setupModel();
  if (!c) {
    const done = S.analysis ? S.analysis.last_closed_candle_time : null;
    box.innerHTML = S.analysis
      ? `<div class="empty" style="padding:10px 0">
           <div style="color:var(--txt);font-weight:600;letter-spacing:.06em">NO CAUSAL SETUP DETECTED</div>
           <div style="margin-top:4px">${esc(S.symbol)} · engine causal path (D1→H4→M15) is authoritative</div>
           <div class="a-t">Analysis completed · last update ${esc(done || "—")}</div></div>`
      : `<div class="empty">MARKET DATA UNAVAILABLE — engine not consulted.</div>`;
    return;
  }
  const bull = c.direction === "BULLISH";
  const ev = c.evidence || {};
  const sw = ev.sweep || {}, csd = ev.csd || {};
  const st = row || { display: "EXECUTION_READY" };
  const rr = (c.reward_risk != null) ? Number(c.reward_risk).toFixed(2) : "—";
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center">
      <b>${esc(c.symbol)} · causal MTF</b><span class="chip ${st.display}">${st.display}</span></div>
    <div class="kv"><span class="k">Direction</span><span class="v ${bull ? "bull" : "bear"}">${bull ? "BULLISH SETUP ▲" : "BEARISH SETUP ▼"}</span></div>
    <div style="color:var(--faint);margin:4px 0 2px">structure evidence (engine)</div>
    <div class="kv"><span class="k">Liquidity sweep</span><span class="v">${sw.side ? esc(sw.side.replace("_SIDE", "").replace("_", "-")) + " @ " + fmt(sw.swept_level) + " → " + fmt(sw.sweep_extreme) : "unavailable"}</span></div>
    <div class="kv"><span class="k">CSD</span><span class="v">${csd.id ? esc(csd.type) + " " + esc((csd.direction || "").slice(0, 4)) + " @ " + fmt(csd.level) : "unavailable"}</span></div>
    <div class="kv"><span class="k">D1 POI</span><span class="v">${ev.poi_id ? esc(ev.poi_id) : "unavailable"}</span></div>
    <div class="kv"><span class="k">Order Block</span><span class="v">${ev.order_block_id ? esc(ev.order_block_id) : "unavailable"}</span></div>
    <div class="kv"><span class="k">Inducement</span><span class="v">${ev.inducement_id ? esc(ev.inducement_id) : "unavailable"}</span></div>
    <div class="kv"><span class="k">IRL target</span><span class="v">${ev.irl_swing_id ? esc(ev.irl_swing_id) : "unavailable"}</span></div>
    <div class="kv"><span class="k">Protected level</span><span class="v">${fmt(c.protected_level)}</span></div>
    <div class="kv"><span class="k">Invalidation</span><span class="v">${fmt(c.invalidation_level)}</span></div>
    <hr style="border-color:var(--line);margin:6px 0">
    <div class="kv"><span class="k">Entry</span><span class="v">${fmt(c.entry)}</span></div>
    <div class="kv"><span class="k">Stop</span><span class="v" style="color:var(--bear)">${fmt(c.stop_loss)}</span></div>
    <div class="kv"><span class="k">Target</span><span class="v" style="color:var(--bull)">${fmt(c.take_profit)}</span></div>
    <div class="kv"><span class="k">R:R</span><span class="v">${rr}</span></div>
    <div class="kv"><span class="k">Risk</span><span class="v">${fmt(c.risk_percent, 2)}%</span></div>
    <div class="kv"><span class="k">Setup ID</span><span class="v" style="color:var(--faint)">${esc(c.id)}</span></div>
    <div style="display:flex;gap:6px">
      <button class="cta ok" id="btn-dry">DRY RUN</button>
      <button class="cta" id="btn-paper" ${S.mode === "PAPER" && S.status.paper_enabled && st.display === "EXECUTION_READY" ? "" : "disabled"}>PAPER EXECUTE</button>
    </div>
    <button class="cta danger hidden" id="btn-cancel">CANCEL PENDING</button>`;
  S.selected = c.id;
  $("#btn-dry").onclick = async () => {
    try { const r = await api(`/api/dry-run/${c.id}`, { method: "POST" });
      toast(`${r.status}${r.order ? ` ${r.order.side} vol=${r.order.volume}` : (r.reason ? " " + r.reason : "")}`); }
    catch (e) { toast("dry-run: " + e.message, "err"); }
  };
  $("#btn-paper").onclick = async () => {
    try { const r = await api(`/api/paper/${c.id}`, { method: "POST" });
      toast(`PAPER: ${r.status} ticket=${r.ticket}`, "ok"); loadSetups(); loadHistory(); }
    catch (e) { toast("paper: " + e.message, "err"); }
  };
  const cancelBtn = $("#btn-cancel");
  if (row && row.ticket != null) {
    cancelBtn.classList.remove("hidden");
    cancelBtn.textContent = `CANCEL PENDING #${row.ticket}`;
    cancelBtn.onclick = async () => {
      try { const r = await api(`/api/paper/${c.id}/cancel`, { method: "POST" });
        toast(`${r.status} ticket ${r.ticket}`); loadSetups(); loadHistory(); }
      catch (e) { toast("cancel: " + e.message, "err"); }
    };
  }
}

async function renderRisk() {
  const box = $("#risk-body");
  try {
    const r = await api(`/api/risk?symbol=${S.symbol}&setup_id=${S.selected || ""}`);
    const ns = r.new_setup;
    box.innerHTML = `
      <div class="kv"><span class="k">Account</span><span class="v">${r.equity ? r.equity.toLocaleString(undefined, {maximumFractionDigits: 0}) + " " + esc(r.currency || "") : "—"}</span></div>
      <div class="kv"><span class="k">Max Risk</span><span class="v">${fmt(r.max_total_risk_percent, 2)}%</span></div>
      <div class="kv"><span class="k">Committed</span><span class="v ${r.committed_risk_percent > 0 ? "warn" : ""}">${fmt(r.committed_risk_percent, 2)}%</span></div>
      <div class="kv"><span class="k">Available</span><span class="v bull">${fmt(r.available_risk_percent, 2)}%</span></div>
      <hr style="border-color:var(--line);margin:6px 0">
      ${ns && !ns.error ? `
      <div class="kv"><span class="k">Setup risk</span><span class="v">${fmt(ns.risk_percent, 2)}%</span></div>
      <div class="kv"><span class="k">Size</span><span class="v">${ns.volume} lots</span></div>
      <div class="kv"><span class="k">Est. loss</span><span class="v" style="color:var(--bear)">${fmt(ns.estimated_loss, 2)}</span></div>
      <div class="kv"><span class="k">R:R</span><span class="v">${fmt(ns.reward_risk, 2)}</span></div>
      <div class="kv"><span class="k">Budget</span><span class="v ${ns.portfolio_allocation === "FIT" ? "bull" : "bear"}">${esc(ns.portfolio_allocation || r.portfolio_allocation || "")}</span></div>`
      : ns && ns.error ? `<div class="kv"><span class="k">Setup</span><span class="v warn">${esc(ns.error)}</span></div>`
      : `<div class="empty">No live setup — sizing shown when engine produces one.</div>`}`;
  } catch (e) { box.innerHTML = `<div class="empty">risk unavailable: ${esc(e.message)}</div>`; }
}

function renderExec() {
  const box = $("#exec-body"); const st = S.status;
  box.innerHTML = `
    <div class="kv"><span class="k">Broker</span><span class="v">${esc(st.account ? st.account.server : "—")}</span></div>
    <div class="kv"><span class="k">Mode</span><span class="v ${st.account_mode === "DEMO" ? "bull" : "warn"}">${esc(st.account_mode || "DISCONNECTED")}</span></div>
    <div class="kv"><span class="k">Symbol</span><span class="v">${esc(S.symbol)}</span></div>
    <div class="kv"><span class="k">Execution</span><span class="v">${esc(S.mode)}</span></div>
    <div class="kv"><span class="k">Preflight</span><span class="v">${esc(S.status.paper_enabled ? "broker order_check armed" : "no demo connection")}</span></div>
    <div class="kv"><span class="k">Live</span><span class="v warn">${st.live_execution_enabled ? "ENABLED" : "DISABLED"}</span></div>`;
}

const LADDER_STEPS = ["WATCHING", "DEVELOPING", "EXECUTION_READY", "ORDER_PREPARED", "ORDER_PLACED", "COMPLETED"];
function renderLifecycle() {
  const box = $("#lifecycle-body");
  renderExec();
  const cur = S.setups.find(s => s.setup_id === S.selected) || S.setups[0];
  if (!cur) { box.innerHTML = `<div class="empty">no registered setups</div>`; return; }
  const inv = cur.display === "INVALIDATED";
  const curIdx = LADDER_STEPS.indexOf(cur.display);
  box.innerHTML = `<div style="margin-bottom:6px"><b>${esc(cur.setup_id.slice(0, 16))}</b>
      <span class="chip ${cur.display}">${cur.display}</span></div>
    <div class="stepper">${LADDER_STEPS.map((s, i) =>
      `<div class="step ${inv ? "" : i < curIdx ? "done" : i === curIdx ? "cur" : ""}"><span class="dot"></span>${s}</div>`
    ).join("")}${inv ? `<div class="step bad"><span class="dot"></span>INVALIDATED${cur.reason ? " · " + esc(cur.reason) : ""}</div>` : ""}</div>`;
}

/* observer (grouped hypotheses) */
function renderObserver(rows) {
  S.hyps = rows;
  const box = $("#observer-body");
  const groups = {};
  for (const h of rows) {
    const k = `${h.symbol} ${h.timeframe}`;
    (groups[k] = groups[k] || []).push(h);
  }
  const keys = Object.keys(groups);
  if (!keys.length) { box.innerHTML = `<div class="empty">No hypotheses yet — add one in WEEKLY PLAN.</div>`; return; }
  box.innerHTML = keys.map(k => `<div style="margin-bottom:6px"><div class="obs-row"><span class="t">${esc(k)}</span></div>
    ${groups[k].map(h => `<div class="obs-row" style="padding-left:12px">
      <span class="chip ${h.state === "INVALIDATED" ? "INVALIDATED" : h.state === "COMPLETED" ? "COMPLETED" : "WATCHING"}">${esc(h.state)}</span>
      <span style="color:var(--dim)"> ${h.bias === "BULLISH" ? "▲" : "▼"} ${esc(h.thesis || "no thesis")}</span></div>`).join("")}</div>`).join("");
}

async function savePlan() {
  const body = {
    symbol: $("#p-symbol").value.trim().toUpperCase(), timeframe: $("#p-tf").value,
    bias: $("#p-bias").value, thesis: $("#p-thesis").value, trigger: $("#p-trigger").value,
    confirmation: $("#p-confirm").value, invalidation: $("#p-invalidate").value, target: $("#p-target").value,
  };
  if (!body.symbol) return toast("symbol required", "err");
  try {
    await api("/api/hypotheses", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#plan-form").classList.add("hidden");
    toast("hypothesis saved"); loadHypotheses();
  } catch (e) { toast(e.message, "err"); }
}

function renderAlerts(rows) {
  $("#alerts-body").innerHTML = (rows || []).length ? rows.slice(0, 40).map(a =>
    `<div class="alert ${esc(a.kind)}"><div>${esc(a.symbol)} <b>${esc(a.kind)}</b> — ${esc(a.message)}</div>
     <div class="a-t">${esc((a.time || "").slice(0, 19))}</div></div>`).join("") : `<div class="empty">none yet</div>`;
}

function renderHistory(rows) {
  S.hist = rows || S.hist || [];
  const sel = $("#f-status");
  const states = [...new Set(S.hist.map(r => r.state))];
  if (sel.options.length <= 1) sel.innerHTML = `<option value="">All statuses</option>` + states.map(s => `<option>${esc(s)}</option>`).join("");
  const fs = sel.value, fd = $("#f-dir").value, fx = $("#f-symbol").value.toUpperCase();
  const tb = $("#hist-table tbody");
  tb.innerHTML = S.hist.filter(r =>
    (!fs || r.state === fs) && (!fd || r.direction === fd) && (!fx || (r.symbol || "").includes(fx)))
    .map(r => `<tr><td>${esc((r.created_time || "").slice(0, 16))}</td><td>${esc(r.symbol)}</td>
      <td style="color:${r.direction === "BULLISH" ? "var(--bull)" : "var(--bear)"}">${esc((r.direction || "").slice(0, 4))}</td>
      <td>${fmt(r.entry)}</td><td>${fmt(r.stop_loss)}</td><td>${fmt(r.take_profit)}</td>
      <td>${r.reward_risk != null ? r.reward_risk : "—"}</td>
      <td>${fmt(r.risk_percent, 2)}%</td><td><span class="chip ${r.state}">${esc(r.state)}</span></td>
      <td>${r.ticket ?? "—"}</td></tr>`).join("") || `<tr><td colspan="10" style="color:var(--faint)">no records</td></tr>`;
}

boot();
