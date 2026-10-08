/* SMC ENGINE workstation frontend.
   View + user intent ONLY. All analytics/risk/execution come from the backend. */
"use strict";

const TF_ORDER = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const S = {
  symbol: null, tf: "M15", analysis: null, setups: [], selected: null,
  mode: "ANALYSIS", status: {}, symbols: [], prices: {}, layers: {},
  risk: { state: "NONE", reason: "" },
  symStates: {},   // symbol → { label, direction } from latest opportunities response
  ctxOpen: false,  // context drawer visibility
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

/* ── Context drawer ──────────────────────────────────────────────────────── */
function openContextDrawer(tab = "setup") {
  const drawer = $("#context-drawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  S.ctxOpen = true;
  switchCtxTab(tab);
}
function closeContextDrawer() {
  const drawer = $("#context-drawer");
  if (drawer) drawer.classList.add("hidden");
  S.ctxOpen = false;
}
function switchCtxTab(tab) {
  $$(".ctx-tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  ["setup","risk","exec","lifecycle"].forEach(t => {
    const pane = $(`#ctx-${t}`);
    if (pane) pane.classList.toggle("hidden", t !== tab);
  });
}
function switchSigTab(tab) {
  $$(".sig-tab").forEach(b => b.classList.toggle("active", b.dataset.sig === tab));
  ["opportunities","alerts","events"].forEach(t => {
    const pane = $(`#sig-${t}`);
    if (pane) pane.classList.toggle("hidden", t !== tab);
  });
}

/* ── Runtime fingerprint ─────────────────────────────────────────────────── */
async function loadRuntime() {
  try {
    const r = await api("/api/runtime");
    S.runtime = r;
    const chip = $("#build-chip");
    if (!chip) return;
    const dirty = r.git_dirty ? "·dirty" : "";
    chip.textContent = `BUILD ${esc(r.git_commit || "?")}${dirty}`;
    chip.title = `git: ${r.git_commit}${r.git_dirty ? " (dirty)" : ""} · PID ${r.pid} · started ${(r.server_started_at || "").slice(0, 19)}Z`;
    if (r.files_modified_after_startup && r.files_modified_after_startup.length) {
      chip.style.color = "var(--warn, #f59e0b)";
      chip.title += ` · STALE: ${r.files_modified_after_startup.join(", ")}`;
    }
    chip.onclick = () => {
      const lines = [
        `git: ${r.git_commit}${r.git_dirty ? " (DIRTY)" : ""}`,
        `pid: ${r.pid}  python: ${(r.python_executable || "").split(/[\\/]/).pop()}`,
        `started: ${(r.server_started_at || "").slice(0, 19)}Z`,
        `live_execution: ${r.live_execution_enabled}`,
        ...(r.files_modified_after_startup || []).map(f => `STALE: ${f}`),
        `--- source hashes ---`,
        ...Object.entries(r.source_hashes || {}).map(([k, v]) => `  ${k}: ${v}`),
      ];
      alert(lines.join("\n"));
    };
  } catch (e) {
    const chip = $("#build-chip");
    if (chip) { chip.textContent = "BUILD ?"; chip.title = "runtime endpoint unavailable"; }
  }
}

/* ── Boot ────────────────────────────────────────────────────────────────── */
async function boot() {
  await loadStatus();
  try { S.symbols = (await api("/api/symbols")).symbols; } catch (e) { S.symbols = []; }
  S.symbol = S.symbols.includes("EURAUD") ? "EURAUD" : (S.symbols[0] || "EURAUD");
  buildTfButtons(); buildSymbolList(); wire();
  openStream();
  loadRuntime();
  await refresh();
}

function refreshAll() { loadStatus(); refresh(); }
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll();
});

function wire() {
  /* TF buttons — use event delegation so rebuilt buttons still work */
  $("#tf-buttons").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tf]");
    if (b) { S.tf = b.dataset.tf; buildTfButtons(); refresh(); }
  });

  /* Structure overlay toggles — initialise layers from checked state */
  document.querySelectorAll("[data-layer]").forEach(c => {
    S.layers[c.dataset.layer] = c.checked;
    c.onchange = () => { S.layers[c.dataset.layer] = c.checked; drawChart(); };
  });

  /* Structure dropdown toggle */
  const structBtn = $("#struct-btn"), structPanel = $("#struct-panel");
  if (structBtn && structPanel) {
    structBtn.onclick = (e) => { e.stopPropagation(); structPanel.classList.toggle("hidden"); };
  }
  document.addEventListener("click", (e) => {
    if (structPanel && !structPanel.contains(e.target) && e.target !== structBtn)
      structPanel.classList.add("hidden");
  });

  /* Mode selector */
  $$('input[name=mode]').forEach(r => r.onchange = () => { S.mode = r.value; renderExec(); renderSetup(); });

  /* Symbol search */
  $("#symbol-search").oninput = (e) => buildSymbolList(e.target.value);

  /* Weekly plan (lives in sys-drawer) */
  $("#p-tf").innerHTML = TF_ORDER.map(t => `<option${t === "H1" ? " selected" : ""}>${t}</option>`).join("");
  $("#p-symbol").value = S.symbol;
  $("#plan-new").onclick = () => $("#plan-form").classList.toggle("hidden");
  $("#p-save").onclick = savePlan;

  /* History filters */
  ["f-status", "f-dir", "f-symbol"].forEach(id => {
    const el = $(`#${id}`); if (el) el.oninput = renderHistory;
  });

  /* System drawer */
  const sysBtn = $("#sys-btn"), sysDrawer = $("#sys-drawer");
  if (sysBtn && sysDrawer) {
    sysBtn.onclick = () => {
      const open = !sysDrawer.classList.contains("hidden");
      sysDrawer.classList.toggle("hidden", open);
      sysBtn.textContent = open ? "SYSTEM ▾" : "SYSTEM ▴";
    };
  }

  /* Context drawer tabs & close */
  $$(".ctx-tab").forEach(b => b.onclick = () => switchCtxTab(b.dataset.tab));
  const ctxClose = $("#ctx-close");
  if (ctxClose) ctxClose.onclick = closeContextDrawer;

  /* Signals panel tabs */
  $$(".sig-tab").forEach(b => b.onclick = () => switchSigTab(b.dataset.sig));

  /* History toggle */
  const histToggle = $("#hist-toggle"), histSection = $("#history"), histClose = $("#hist-close");
  if (histToggle && histSection) {
    histToggle.onclick = () => {
      const open = histSection.classList.toggle("hidden") === false;
      histToggle.textContent = open ? "HISTORY ▴" : "HISTORY";
    };
  }
  if (histClose && histSection) {
    histClose.onclick = () => { histSection.classList.add("hidden"); if (histToggle) histToggle.textContent = "HISTORY"; };
  }

  /* Mobile signals float button */
  const sigFloat = $("#sig-float-btn"), rightbar = $("#rightbar");
  if (sigFloat && rightbar) {
    sigFloat.onclick = () => rightbar.classList.toggle("open");
    document.addEventListener("click", (e) => {
      if (rightbar && rightbar.classList.contains("open") &&
          !rightbar.contains(e.target) && e.target !== sigFloat)
        rightbar.classList.remove("open");
    });
  }

  setInterval(() => { $("#clock").textContent = new Date().toISOString().slice(11, 19) + "Z"; }, 1000);
  setInterval(() => { if (!document.hidden) loadAnalysis(true); }, 20000);
  setInterval(() => { if (!document.hidden) { loadReadiness(); loadCausalEvents(); loadOpportunities(); } }, 45000);
}

/* ── Data loads ──────────────────────────────────────────────────────────── */
async function refresh() {
  await Promise.all([
    loadAnalysis(), loadSetups(), loadAlerts(),
    loadHypotheses(), loadHistory(), loadReadiness(),
    loadCausalEvents(), loadOpportunities(),
  ]);
}

let analysisSeq = 0;
async function loadAnalysis(quiet) {
  const my = ++analysisSeq;
  const sym = S.symbol, tf = S.tf;
  try {
    const a = await api(`/api/analysis/${sym}/${tf}?count=250`);
    if (my !== analysisSeq) return;
    S.analysis = a;
    S.prices[sym] = a.last_closed_close;
    drawChart(); renderQuote();
    await renderRisk();
    renderSetup(); renderLifecycle();
  } catch (e) {
    if (my !== analysisSeq) return;
    S.analysis = null;
    S.risk = { state: "UNAVAILABLE", reason: e.message };
    renderQuote(null, e.message);
    renderSetup(); renderLifecycle(); drawChart();
  }
}

async function loadStatus() {
  try {
    S.status = await api("/api/status");
  } catch (e) {
    S.status = {
      engine: "UNKNOWN", mt5: "DISCONNECTED", account: null,
      account_mode: "UNKNOWN", market_data: "DOWN", risk_engine: "STANDBY",
      execution: "DISABLED", live_execution_enabled: false,
      paper_enabled: false, source: "none",
    };
  }
  renderStatus(); renderExec();
}

function identityChip(id) {
  if (!id) return "";
  const cls = id.account_class === "DEMO" ? "COMPLETED"
            : id.account_class === "LIVE" ? "INVALIDATED" : "WATCHING";
  return `<span class="q">SERVER <b>${esc(id.login || "—")} @ ${esc(id.server || "none")}</b> ` +
         `<span class="chip ${cls}">${esc(id.account_class || "?")}</span></span>`;
}

function renderQuote(a, err) {
  const el = $("#quote-strip");
  if (!el) return;
  if (!a) {
    const isUnauth = err && (err.includes("403") || err.includes("UNAUTHORIZED_ACCOUNT"));
    el.innerHTML = isUnauth
      ? `<span class="q down">ACCOUNT UNAUTHORIZED — analysis blocked. Log MT5 into authorized demo account (477217728 @ Exness-MT5Trial9).</span>`
      : `<span class="q down">DATA UNAVAILABLE${err ? " — " + esc(err) : ""}</span>`;
    return;
  }
  const q = a.quote || { market_data: "UNAVAILABLE" };
  const head = `<span class="q">${esc(a.symbol)} · ${esc(a.timeframe)}</span>` + identityChip(a.identity);
  if (q.market_data === "AVAILABLE") {
    el.innerHTML = head +
      `<span class="q">BID <b>${fmt(q.bid)}</b></span>` +
      `<span class="q">ASK <b>${fmt(q.ask)}</b></span>` +
      `<span class="q">SPREAD <b>${fmt(q.spread)}</b></span>` +
      `<span class="q">LAST UPDATE <b>${esc(q.tick_time || a.last_closed_candle_time)}</b></span>` +
      `<span class="q">SOURCE <b>${esc(q.source)}</b></span>`;
  } else if (q.market_data === "WAITING_FOR_LIVE_TICK") {
    el.innerHTML = head +
      `<span class="q wait">WAITING FOR LIVE TICK</span>` +
      `<span class="q">historical data available — awaiting current quote</span>`;
  } else {
    el.innerHTML = head + `<span class="q down">MARKET DATA UNAVAILABLE</span>`;
  }
}

async function loadSetups() {
  try {
    const url = `/api/setups?symbol=${encodeURIComponent(S.symbol)}`;
    S.setups = (await api(url)).setups || [];
  } catch (e) { S.setups = []; }
  if (S.selected && !S.setups.find(s => s.setup_id === S.selected)) S.selected = null;
  renderLifecycle(); renderSetup();
}
async function loadAlerts() {
  try {
    const r = await api("/api/alerts?limit=50");
    S.alertStartupTime = r.server_startup_time || null;
    renderAlerts(r.alerts);
  } catch (e) {}
}
async function loadHypotheses() {
  try { renderObserver((await api("/api/hypotheses")).hypotheses); } catch (e) {}
}
async function loadHistory() {
  try { renderHistory((await api("/api/history")).rows); } catch (e) {}
}

/* ── Opportunities ───────────────────────────────────────────────────────── */
async function loadOpportunities() {
  try {
    const r = await api("/api/opportunities?active_only=true");
    renderOpportunities(r.opportunities || [], r.funnel || {});
  } catch (e) {}
}

function _oppAge(iso) {
  if (!iso) return "";
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  return m < 60 ? `${Math.max(0, Math.round(m))}m` : `${(m / 60).toFixed(1)}h`;
}

function renderOpportunities(rows, funnel) {
  /* Funnel strip */
  const strip = $("#opp-funnel-strip");
  const badge = $("#opp-badge");
  const readyCount = funnel.ready_opportunity_count ?? 0;
  const devCount = (funnel.waiting_for_confirmation || 0) +
                   (funnel.waiting_for_poi || 0) +
                   (funnel.waiting_for_idm || 0);
  if (strip) strip.innerHTML = (readyCount || devCount)
    ? `<span class="funnel-stat">● ${readyCount} ready</span>` +
      `<span class="funnel-stat-dim">◐ ${devCount} developing</span>` +
      `<span class="funnel-stat-dim">inv ${funnel.invalidated_count ?? 0}</span>`
    : "";
  if (badge) badge.textContent = readyCount || "";

  /* Build symStates for watchlist indicators */
  S.symStates = {};
  const prio = { READY: 3, ARMED: 2, DEVELOPING: 2, WATCHING: 1 };
  for (const o of rows) {
    const existing = S.symStates[o.symbol];
    if (!existing || (prio[o.label] || 0) > (prio[existing.label] || 0)) {
      S.symStates[o.symbol] = { label: o.label, direction: o.direction };
    }
  }
  buildSymbolList(); // refresh watchlist state indicators

  const box = $("#opp-body");
  if (!rows.length) {
    box.innerHTML = `<div class="empty" style="padding:10px">No developing opportunities — observation only.</div>`;
    return;
  }

  /* Group by priority: READY → DEVELOPING/ARMED → WATCHING */
  const groups = { READY: [], DEVELOPING: [], WATCHING: [] };
  for (const o of rows) {
    if (o.label === "READY") groups.READY.push(o);
    else if (o.label === "ARMED" || o.label === "DEVELOPING") groups.DEVELOPING.push(o);
    else groups.WATCHING.push(o);
  }

  let html = "";
  for (const [gLabel, gRows] of [["READY", groups.READY], ["DEVELOPING", groups.DEVELOPING], ["WATCHING", groups.WATCHING]]) {
    if (!gRows.length) continue;
    html += `<div class="sig-group-h">${gLabel} · ${gRows.length}</div>`;
    for (const o of gRows) {
      const bull = o.direction === "BULLISH";
      html += `<div class="sig-card ${esc(o.label)}" data-sym="${esc(o.symbol)}">
        <div class="sig-card-head">
          <span class="sig-card-sym">${esc(o.symbol)}</span>
          <span class="sig-card-dir ${bull ? "bull" : "bear"}">${bull ? "▲ LONG" : "▼ SHORT"}</span>
          <span class="sig-card-state ${esc(o.label)}">${esc(o.label)}</span>
        </div>
        <div class="sig-card-type">${esc(o.type || "")}${o.entry_pathway ? " · " + esc(o.entry_pathway) : ""}</div>
        ${o.blocker ? `<div class="sig-card-blocker">${esc(o.blocker)}</div>` : ""}
        <div class="sig-card-age">${_oppAge(o.created_at)}</div>
      </div>`;
    }
  }
  box.innerHTML = html;

  box.querySelectorAll("[data-sym]").forEach(el =>
    el.onclick = () => {
      S.symbol = el.dataset.sym;
      const ps = $("#p-symbol"); if (ps) ps.value = S.symbol;
      buildSymbolList(); refresh();
      openContextDrawer("setup");
      /* Close mobile rightbar overlay after selection */
      const rb = $("#rightbar"); if (rb) rb.classList.remove("open");
    });
}

/* ── Gate B readiness ────────────────────────────────────────────────────── */
let _lastGateBStatus = null;
let readinessPrimed = false;

async function loadReadiness() {
  try {
    const r = await api(`/api/readiness${S.symbol ? "?symbol=" + encodeURIComponent(S.symbol) : ""}`);
    S.readiness = r;
    renderReadiness(r);
    const first = !readinessPrimed; readinessPrimed = true;
    for (const id of (r.detected_ids || [])) maybeNotifySetup(id, first);
  } catch (e) {
    renderReadiness({ status: "READINESS UNAVAILABLE (read-only layer)", setup: null, detected_ids: [] });
  }
}

function maybeNotifySetup(id, silent) {
  try {
    const seen = JSON.parse(localStorage.getItem("smc_seen_setups") || "[]");
    if (seen.includes(id)) return;
    seen.push(id);
    localStorage.setItem("smc_seen_setups", JSON.stringify(seen.slice(-200)));
    if (!silent) toast(`CAUSAL SETUP DETECTED — ${id}. Gate B: ready for review.`);
  } catch (e) {}
}

function renderReadiness(r) {
  const chip = $("#gateb-chip");
  if (!chip) return;
  const ready   = r.status === "READY_FOR_MANUAL_VALIDATION";
  const blocked = r.status === "UNAUTHORIZED_ACCOUNT";

  chip.className = ready ? "gateb-ready" : (blocked ? "gateb-blocked" : "gateb-waiting");

  const statusEl = $("#gateb-status"), detailEl = $("#gateb-detail");
  if (statusEl) statusEl.textContent = ready
    ? "GATE B · READY"
    : (blocked ? "GATE B · BLOCKED" : "GATE B · WAITING");
  if (detailEl) detailEl.textContent = ready
    ? `${r.setup ? r.setup.symbol + " · " + r.setup.direction : ""} · awaiting human review`
    : (blocked
        ? "Log MT5 into authorized account (477217728 @ Exness-MT5Trial9 DEMO)."
        : "observation only — detection never executes");

  const btn = $("#gateb-review");
  if (btn) {
    btn.classList.toggle("hidden", !ready);
    if (ready) btn.onclick = () => reviewSetup(r.setup.setup_id);
  }

  /* Open context drawer on first transition to READY */
  if (ready && _lastGateBStatus !== "READY_FOR_MANUAL_VALIDATION") {
    openContextDrawer("setup");
  }
  _lastGateBStatus = r.status;
}

function reviewSetup(id) {
  S.selected = id;
  openContextDrawer("setup");
  renderSetup(); renderLifecycle(); renderRisk();
  api(`/api/setup-history/${encodeURIComponent(id)}/review`, { method: "POST" })
    .then(() => loadCausalEvents()).catch(() => {});
}

/* ── Causal setup event history ──────────────────────────────────────────── */
async function loadCausalEvents() {
  try {
    const r = await api(`/api/setup-history?symbol=${encodeURIComponent(S.symbol)}&limit=25`);
    renderCausalEvents(r.events || []);
  } catch (e) { renderCausalEvents(null, e.message); }
}

function renderCausalEvents(events, err) {
  const box = $("#causal-events");
  if (!box) return;
  if (err) { box.innerHTML = `<div class="empty">history unavailable: ${esc(err)}</div>`; return; }
  if (!events.length) {
    box.innerHTML = `<div class="empty" style="padding:10px">No causal setup events yet.<br>History fills when a genuine SETUP-* appears.</div>`;
    $("#causal-detail").classList.add("hidden");
    return;
  }
  box.innerHTML = events.map(ev => `
    <div class="sig-alert ${ev.status === "ACTIVE" ? "SETUP_CREATED" : ev.status === "CLOSED" ? "" : "SETUP_INVALIDATED"}"
         data-causal="${esc(ev.setup_id)}" style="cursor:pointer">
      <div class="alert-body">
        <div class="alert-kind">${esc(ev.setup_id.slice(0, 22))}</div>
        <div class="alert-sym">${esc(ev.symbol)} · ${esc(ev.direction)}
          <span class="chip ${ev.status === "ACTIVE" ? "EXECUTION_READY" : ev.status === "CLOSED" ? "COMPLETED" : "INVALIDATED"}" style="margin-left:4px">${esc(ev.status)}</span>
          ${ev.close_reason ? " · " + esc(ev.close_reason) : ""}
        </div>
      </div>
      <span class="age">${esc((ev.first_seen_at || "").slice(11, 16))}</span>
    </div>`).join("");
  box.querySelectorAll("[data-causal]").forEach(el =>
    el.onclick = () => openCausalDetail(el.dataset.causal));
}

async function openCausalDetail(id) {
  const box = $("#causal-detail");
  try {
    const { event } = await api(`/api/setup-history/${encodeURIComponent(id)}`);
    const t = event.timeline || [];
    box.classList.remove("hidden");
    box.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b style="font-size:11.5px">${esc(event.setup_id)}</b>
        <button class="mini" id="causal-close">close</button></div>
      <div class="kv"><span class="k">Symbol / TF</span><span class="v">${esc(event.symbol)} · ${esc(event.timeframe)}</span></div>
      <div class="kv"><span class="k">Direction</span><span class="v ${event.direction === "BULLISH" ? "bull" : "bear"}">${esc(event.direction)}</span></div>
      <div class="kv"><span class="k">Entry</span><span class="v">${fmt(event.entry)}</span></div>
      <div class="kv"><span class="k">Stop</span><span class="v" style="color:var(--bear)">${fmt(event.stop_loss)}</span></div>
      <div class="kv"><span class="k">Target</span><span class="v" style="color:var(--bull)">${fmt(event.take_profit)}</span></div>
      <div class="kv"><span class="k">R:R</span><span class="v">${event.rr ?? "—"}</span></div>
      <div class="kv"><span class="k">Risk</span><span class="v">${fmt(event.risk_percent, 2)}%</span></div>
      <div class="kv"><span class="k">As-of</span><span class="v">${esc(event.as_of || "—")}</span></div>
      <div class="kv"><span class="k">First seen</span><span class="v">${esc(event.first_seen_at || "")}</span></div>
      <div class="kv"><span class="k">Last seen</span><span class="v">${esc(event.last_seen_at || "")} · ${event.observations} obs</span></div>
      <div class="kv"><span class="k">Status</span><span class="v">${esc(event.status)}${event.close_reason ? " · " + esc(event.close_reason) : ""}</span></div>
      <div class="kv"><span class="k">Reviewed</span><span class="v">${event.review_count ? `${event.review_count}× since ${esc((event.reviewed_at || "").slice(0, 19))}` : "not yet"}</span></div>
      <div class="kv"><span class="k">Paper execution</span><span class="v">${esc(event.paper_execution_outcome || "NONE")}</span></div>
      <div style="color:var(--faint);margin:4px 0 2px">evidence (backend snapshot)</div>
      <pre class="ev-json">${esc(JSON.stringify(event.evidence, null, 1))}</pre>
      <div style="color:var(--faint);margin:4px 0 2px">timeline</div>
      ${t.map(x => `<div class="a-t">${esc((x.at || "").slice(11, 19))}Z · ${esc(x.kind)} · ${esc(x.detail)}</div>`).join("")}
      <button class="cta" id="causal-review">REVIEW SETUP</button>`;
    $("#causal-close").onclick = () => box.classList.add("hidden");
    $("#causal-review").onclick = () => reviewSetup(event.setup_id);
  } catch (e) {
    box.classList.remove("hidden");
    box.innerHTML = `<div class="empty">detail unavailable: ${esc(e.message)}</div>`;
  }
}

/* ── SSE stream ──────────────────────────────────────────────────────────── */
function openStream() {
  const es = new EventSource("/api/events");
  es.addEventListener("MARKET_UPDATE", (ev) => {
    const p = JSON.parse(ev.data).payload;
    if (p.symbol === S.symbol && p.price) { S.prices[p.symbol] = p.price; updatePriceTag(p.price); }
  });
  ["SETUP_CREATED", "SETUP_UPDATED", "SETUP_INVALIDATED"].forEach(k =>
    es.addEventListener(k, () => {
      loadSetups(); loadReadiness(); loadCausalEvents();
      if (k !== "SETUP_UPDATED") loadAnalysis(true);
    }));
  ["ORDER_PREPARED", "ORDER_PLACED", "ORDER_CANCELLED"].forEach(k =>
    es.addEventListener(k, (ev) => {
      loadSetups(); loadHistory(); loadCausalEvents();
      toast(`${k}: ${JSON.parse(ev.data).payload.setup_id || ""}`);
    }));
  es.addEventListener("ALERT_CREATED", () => loadAlerts());
  ["OPPORTUNITY_CREATED", "OPPORTUNITY_ADVANCED", "POI_FOUND", "IDM_CONFIRMED", "READY",
   "INVALIDATED", "EXPIRED", "CONVERTED_TO_SETUP"].forEach(k =>
    es.addEventListener(k, (ev) => {
      try {
        const p = JSON.parse(ev.data).payload || {};
        toast(`${k}: ${p.symbol || ""} ${p.state || ""}`);
      } catch (e) {}
      loadOpportunities(); loadAlerts();
    }));
  es.onerror = () => {
    const b = $("#conn-badge");
    if (b) { b.textContent = "STREAM DOWN"; b.className = "badge err"; }
  };
  es.onopen = () => renderStatus();
}

/* ── Status / health ─────────────────────────────────────────────────────── */
function renderStatus() {
  const st = S.status;
  const unauth = st.account_identity === "UNAUTHORIZED";

  /* Subsystem details (in sys-drawer) */
  const sEngine = $("#s-engine");
  if (sEngine) { sEngine.textContent = st.engine || "–"; sEngine.style.color = unauth ? "var(--err, #f87171)" : ""; }
  const sMT5 = $("#s-mt5"); if (sMT5) sMT5.textContent = st.mt5 || "–";
  const sAcct = $("#s-acct"); if (sAcct) sAcct.textContent = st.account_mode || "–";
  const sMD = $("#s-md"); if (sMD) sMD.textContent = st.market_data || "–";
  const sRisk = $("#s-risk"); if (sRisk) sRisk.textContent = st.risk_engine || "–";
  const sExec = $("#s-exec"); if (sExec) sExec.textContent = st.live_execution_enabled ? "LIVE-ON" : (st.execution || "–");

  /* Health chip in header */
  const hChip = $("#health-chip");
  if (hChip) {
    const connected = st.mt5 === "CONNECTED";
    const mdOk = st.market_data && !["DOWN", "UNAVAILABLE", "FAILED"].includes(st.market_data);
    if (unauth) {
      hChip.textContent = "⛔ UNAUTHORIZED"; hChip.className = "health-err";
    } else if (!connected) {
      hChip.textContent = "○ DISCONNECTED"; hChip.className = "health-unknown";
    } else if (!mdOk) {
      hChip.textContent = "⚠ DATA DEGRADED"; hChip.className = "health-warn";
    } else {
      hChip.textContent = "● DATA HEALTHY"; hChip.className = "health-ok";
    }
  }

  /* Account badge */
  const badge = $("#account-badge");
  if (badge) {
    if (unauth) {
      badge.textContent = "UNAUTHORIZED"; badge.className = "badge err";
    } else {
      badge.textContent = st.account ? `${st.account_mode} · ${st.account.server}` : "NO ACCOUNT";
      badge.className = "badge " + (st.account_mode === "DEMO" ? "demo" : st.account_mode === "LIVE" ? "live" : "");
    }
  }

  /* Connection badge */
  const cBadge = $("#conn-badge");
  if (cBadge) {
    cBadge.textContent = st.mt5 === "CONNECTED" ? "CONNECTED" : "DISCONNECTED";
    cBadge.className = "badge " + (st.mt5 === "CONNECTED" ? "demo" : "err");
  }

  /* Unauthorized banner */
  const banner = $("#unauth-banner");
  if (banner) {
    banner.style.display = unauth ? "block" : "none";
    if (unauth) {
      const accts = $("#unauth-accounts");
      if (accts) accts.textContent =
        `Expected: ${st.authorized_account || "477217728@Exness-MT5Trial9 DEMO"}  ·  Connected: ${st.actual_account || "unknown"}`;
      const instr = $("#unauth-instruction");
      if (instr) instr.textContent =
        "Log MT5 terminal into the authorized Exness demo account (477217728 @ Exness-MT5Trial9) before analysis can run.";
    }
  }
}

function updatePriceTag(p) {
  const el = $("#chart-price");
  const prev = parseFloat(el.dataset.v || p);
  el.textContent = fmt(p); el.dataset.v = p;
  el.className = "price " + (p >= prev ? "up" : "down");
}

/* ── Symbol watchlist ────────────────────────────────────────────────────── */
function buildSymbolList(filter = "") {
  const box = $("#symbol-list"); if (!box) return; box.innerHTML = "";
  const f = filter.toUpperCase();
  const majors = ["EURUSD","GBPUSD","USDJPY","XAUUSD","XAGUSD","EURAUD"];
  const list = S.symbols.filter(s => s.includes(f) && !/^[A-Z]{2}\d/.test(s));
  const ordered = [...majors.filter(m => list.includes(m)), ...list.filter(s => !majors.includes(s))];
  for (const sym of ordered.slice(0, 300)) {
    const d = document.createElement("div");
    d.className = "sym" + (sym === S.symbol ? " sel" : "");
    const st = S.symStates[sym];
    const dot = st ? (st.label === "READY" ? "●" : "◐") : "—";
    const dotCls = st ? (st.label === "READY" ? "sym-dot-ready" : "sym-dot-dev") : "sym-dot-none";
    const dir = st ? (st.direction === "BULLISH" ? "▲" : "▼") : "";
    const dirCls = st ? (st.direction === "BULLISH" ? "bull" : "bear") : "";
    d.innerHTML =
      `<span class="sym-name">${esc(sym)}</span>` +
      `<span class="sym-state-col">` +
      (dir ? `<span class="sym-dir-glyph ${dirCls}">${dir}</span>` : "") +
      `<span class="${dotCls}">${dot}</span></span>`;
    d.onclick = () => { S.symbol = sym; const ps = $("#p-symbol"); if (ps) ps.value = sym; buildSymbolList(filter); refresh(); };
    box.appendChild(d);
  }
}

function buildTfButtons() {
  const el = $("#tf-buttons"); if (!el) return;
  el.innerHTML = TF_ORDER.map(t =>
    `<button data-tf="${t}" class="${t === S.tf ? "sel" : ""}">${t}</button>`).join("");
}

/* ── Chart ───────────────────────────────────────────────────────────────── */
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
  const setup = setupModel().cand;
  if (setup) { hi = Math.max(hi, setup.take_profit, setup.entry); lo = Math.min(lo, setup.stop_loss, setup.entry); }
  const rng = hi - lo || 1; hi += rng * 0.06; lo -= rng * 0.06;
  const X = (i) => padL + (i - off + 0.5) * cw;
  const Y = (p) => padT + (hi - p) / (hi - lo) * (H - padT - padB);

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

  if (L("fvgs")) for (const g of a.fvgs) {
    const i0 = timeIdx.get(g.time); if (i0 == null) continue;
    const col = g.direction === "BULLISH" ? "rgba(45,212,167," : "rgba(255,93,115,";
    cx.fillStyle = col + (g.state === "MITIGATED" ? "0.04)" : "0.11)");
    cx.fillRect(X(i0), Y(g.top), Math.max(6, W - padR - X(i0)), Y(g.bottom) - Y(g.top));
  }
  if (L("pois")) for (const p of a.pois) {
    const i0 = nearestIdx(p); if (i0 == null) continue;
    cx.fillStyle = p.direction === "BULLISH" ? "rgba(45,212,167,.07)" : "rgba(255,93,115,.07)";
    cx.fillRect(X(i0), Y(p.high), W - padR - X(i0), Y(p.low) - Y(p.high));
  }
  if (L("ob")) for (const b of a.order_blocks) {
    const i0 = timeIdx.get(b.time); if (i0 == null) continue;
    cx.strokeStyle = b.direction === "BULLISH" ? "rgba(45,212,167,.55)" : "rgba(255,93,115,.55)";
    cx.setLineDash([2, 3]); cx.strokeRect(X(i0), Y(b.high), Math.max(8, (off + vis - i0) * cw * 0.6), Y(b.low) - Y(b.high));
    cx.setLineDash([]);
  }
  for (let i = 0; i < vis; i++) {
    const [, o, h, l, c] = candles[i]; const x = X(off + i);
    cx.strokeStyle = cx.fillStyle = c >= o ? "#26a69a" : "#ef5350";
    cx.beginPath(); cx.moveTo(x, Y(h)); cx.lineTo(x, Y(l)); cx.stroke();
    const bw = Math.max(1.5, cw * 0.62);
    cx.fillRect(x - bw / 2, Math.min(Y(o), Y(c)), bw, Math.max(1, Math.abs(Y(c) - Y(o))));
  }
  if (L("swings")) for (const s of a.swings) {
    if (s.index < off) continue;
    const x = X(s.index), y = Y(s.price);
    cx.fillStyle = s.type === "HIGH" ? "#ef5350" : "#26a69a";
    cx.beginPath(); cx.arc(x, s.type === "HIGH" ? y - 6 : y + 6, 2.4, 0, 7); cx.fill();
  }
  if (L("sweeps")) for (const w of a.sweeps) {
    const i0 = timeIdx.get(w.time); if (i0 == null || i0 < off) continue;
    const x = X(i0), y = Y(w.sweep_extreme);
    cx.fillStyle = w.side === "SELL_SIDE" ? "#2dd4a7" : "#ff5d73";
    cx.beginPath(); cx.arc(x, y, 4, 0, 7); cx.fill();
    cx.strokeStyle = cx.fillStyle; cx.setLineDash([2, 2]);
    cx.beginPath(); cx.moveTo(padL, Y(w.swept_level)); cx.lineTo(x, Y(w.swept_level)); cx.stroke(); cx.setLineDash([]);
  }
  if (L("events")) for (const e of a.structure_events) {
    const i0 = timeIdx.get(e.time); if (i0 == null || i0 < off) continue;
    const y = Y(e.level), bull = e.direction === "BULLISH";
    cx.strokeStyle = bull ? "#2dd4a7" : "#ff5d73"; cx.setLineDash([5, 3]);
    cx.beginPath(); cx.moveTo(X(i0), y); cx.lineTo(W - padR, y); cx.stroke(); cx.setLineDash([]);
    cx.fillStyle = bull ? "#2dd4a7" : "#ff5d73"; cx.font = "9.5px monospace";
    cx.fillText(`${e.type}${bull ? "↑" : "↓"} ${e.level.toFixed(4)}`, X(i0) + 2, y - 3);
  }
  if (setup && L("setup")) {
    hline(setup.entry, "#4f8cff", `ENTRY ${fmt(setup.entry)}`);
    hline(setup.stop_loss, "#ef5350", `STOP ${fmt(setup.stop_loss)}`);
    hline(setup.take_profit, "#26a69a", `TARGET ${fmt(setup.take_profit)}`);
    cx.fillStyle = "rgba(79,140,255,.07)";
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

/* Crosshair */
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

/* ── Context drawer panels ───────────────────────────────────────────────── */
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
           <div class="a-t">last update ${esc(done || "—")}</div></div>`
      : `<div class="empty">MARKET DATA UNAVAILABLE — engine not consulted.</div>`;
    return;
  }
  const bull = c.direction === "BULLISH";
  const ev = c.evidence || {};
  const sw = ev.sweep || {}, csd = ev.csd || {};
  const st = row || { display: "EXECUTION_READY" };
  const rr = (c.reward_risk != null) ? Number(c.reward_risk).toFixed(2) : "—";

  /* Causal chain progress */
  const chain = [];
  if (ev.poi_id)        chain.push("D1 POI ✓");
  if (sw.side)          chain.push("H4 SWEEP ✓");
  if (csd.id)           chain.push("H4 CSD ✓");
  if (ev.order_block_id) chain.push("M15 OB ✓");
  if (ev.inducement_id) chain.push("M15 IDM ✓");
  if (ev.irl_swing_id)  chain.push("IRL ✓");

  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <b>${esc(c.symbol)} ${bull ? "▲ LONG" : "▼ SHORT"}</b>
      <span class="chip ${st.display}">${st.display}</span>
    </div>
    <div style="color:var(--dim);font-size:10.5px;margin-bottom:6px">${chain.join(" → ")}</div>
    <div class="kv"><span class="k">Entry</span><span class="v">${fmt(c.entry)}</span></div>
    <div class="kv"><span class="k">Stop</span><span class="v" style="color:var(--bear)">${fmt(c.stop_loss)}</span></div>
    <div class="kv"><span class="k">Target</span><span class="v" style="color:var(--bull)">${fmt(c.take_profit)}</span></div>
    <div class="kv"><span class="k">R:R</span><span class="v">${rr}</span></div>
    <div class="kv"><span class="k">Risk</span><span class="v">${fmt(c.risk_percent, 2)}%</span></div>
    <details style="margin-top:6px"><summary style="color:var(--faint);font-size:10.5px;cursor:pointer">EVIDENCE</summary>
      <div class="kv"><span class="k">Sweep</span><span class="v">${sw.side ? esc(sw.side.replace("_SIDE","").replace("_","-")) + " @ " + fmt(sw.swept_level) : "—"}</span></div>
      <div class="kv"><span class="k">CSD</span><span class="v">${csd.id ? esc(csd.type) + " @ " + fmt(csd.level) : "—"}</span></div>
      <div class="kv"><span class="k">Protected</span><span class="v">${fmt(c.protected_level)}</span></div>
      <div class="kv"><span class="k">Invalidation</span><span class="v">${fmt(c.invalidation_level)}</span></div>
      <div style="color:var(--faint);font-size:10px;margin-top:3px;word-break:break-all">${esc(c.id)}</div>
    </details>
    <div style="display:flex;gap:6px;margin-top:8px">
      <button class="cta ok" id="btn-dry">DRY RUN</button>
      <button class="cta" id="btn-paper" ${S.mode === "PAPER" && S.status.paper_enabled && st.display === "EXECUTION_READY" && S.risk.for === c.id && S.risk.state === "OK" ? "" : "disabled"}>PAPER EXECUTE</button>
    </div>
    ${st.display === "EXECUTION_READY" && S.risk.for === c.id && S.risk.state === "UNAVAILABLE"
        ? `<div class="risk-block">PAPER EXECUTION BLOCKED — RISK CHECK UNAVAILABLE</div>`
        : st.display === "EXECUTION_READY" && S.risk.for === c.id && S.risk.state === "REJECTED"
        ? `<div class="risk-block">PAPER EXECUTION BLOCKED — ${esc(S.risk.reason)}</div>`
        : st.display === "EXECUTION_READY" && S.risk.for !== c.id
        ? `<div class="risk-pending">checking risk…</div>` : ""}
    <button class="cta danger hidden" id="btn-cancel">CANCEL PENDING</button>`;
  S.selected = c.id;
  if (st.display === "EXECUTION_READY" && S.risk.for !== c.id && S.risk._req !== c.id) {
    S.risk._req = c.id; renderRisk();
  }
  $("#btn-dry").onclick = async () => {
    try {
      const r = await api(`/api/dry-run/${c.id}`, { method: "POST" });
      toast(`${r.status}${r.order ? ` ${r.order.side} vol=${r.order.volume}` : (r.reason ? " " + r.reason : "")}`);
    } catch (e) { toast("dry-run: " + e.message, "err"); }
  };
  $("#btn-paper").onclick = async () => {
    try {
      const r = await api(`/api/paper/${c.id}`, { method: "POST" });
      toast(`PAPER: ${r.status} ticket=${r.ticket}`, "ok"); loadSetups(); loadHistory();
    } catch (e) { toast("paper: " + e.message, "err"); }
  };
  const cancelBtn = $("#btn-cancel");
  if (row && row.ticket != null) {
    cancelBtn.classList.remove("hidden");
    cancelBtn.textContent = `CANCEL PENDING #${row.ticket}`;
    cancelBtn.onclick = async () => {
      try {
        const r = await api(`/api/paper/${c.id}/cancel`, { method: "POST" });
        toast(`${r.status} ticket ${r.ticket}`); loadSetups(); loadHistory();
      } catch (e) { toast("cancel: " + e.message, "err"); }
    };
  }
}

async function renderRisk() {
  const box = $("#risk-body");
  try {
    const r = await api(`/api/risk?symbol=${S.symbol}&setup_id=${S.selected || ""}`);
    if (!S.selected) S.risk = { state: "NONE", reason: "", for: null };
    else if (r.status === "RISK_OK") S.risk = { state: "OK", reason: "", for: S.selected };
    else if (r.status === "RISK_REJECTED" || r.status === "RISK_PORTFOLIO_REJECTED")
      S.risk = { state: "REJECTED", for: S.selected,
                 reason: (r.new_setup && r.new_setup.error) || (r.validation && r.validation.error) || r.status };
    else if (r.new_setup && r.new_setup.error)
      S.risk = { state: "REJECTED", for: S.selected, reason: r.new_setup.error };
    else if (r.new_setup) S.risk = { state: "OK", reason: "", for: S.selected };
    else S.risk = { state: "UNAVAILABLE", for: S.selected, reason: r.compute_error || r.status || "no risk result" };
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
  } catch (e) {
    S.risk = { state: "UNAVAILABLE", reason: e.message, for: S.selected || null };
    box.innerHTML = `<div class="empty">risk unavailable: ${esc(e.message)}</div>`;
  }
  renderSetup();
}

function renderExec() {
  const box = $("#exec-body"); if (!box) return;
  const st = S.status;
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
  const box = $("#lifecycle-body"); if (!box) return;
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

/* ── Market observer / weekly plan (in sys-drawer) ───────────────────────── */
function renderObserver(rows) {
  S.hyps = rows;
  const box = $("#observer-body"); if (!box) return;
  const groups = {};
  for (const h of rows) {
    const k = `${h.symbol} ${h.timeframe}`;
    (groups[k] = groups[k] || []).push(h);
  }
  const keys = Object.keys(groups);
  if (!keys.length) { box.innerHTML = `<div class="empty">No hypotheses — add one in WEEKLY PLAN.</div>`; return; }
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

/* ── Alerts ──────────────────────────────────────────────────────────────── */
function _alertAge(iso) {
  if (!iso) return "";
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  return m < 60 ? `${Math.max(0, Math.round(m))}m` : `${(m / 60).toFixed(1)}h`;
}

function renderAlerts(rows) {
  const startup = S.alertStartupTime || null;
  const badge = $("#alerts-badge");
  const recentCount = (rows || []).filter(a => !startup || a.time >= startup).length;
  if (badge) badge.textContent = recentCount || "";
  const box = $("#alerts-body"); if (!box) return;
  box.innerHTML = (rows || []).length ? rows.slice(0, 40).map(a => {
    const historical = startup && a.time < startup;
    return `<div class="sig-alert ${esc(a.kind)}${historical ? " stale" : ""}">
      <div class="alert-body">
        <div class="alert-kind">${historical ? `<span class="a-hist">HIST</span> ` : ""}${esc(a.kind.replace(/_/g, " "))}</div>
        <div class="alert-sym">${esc(a.symbol)}${a.message ? ` — ${esc(a.message)}` : ""}</div>
      </div>
      <span class="age">${_alertAge(a.time)}</span>
    </div>`;
  }).join("") : `<div class="empty" style="padding:10px">none yet</div>`;
}

/* ── History ─────────────────────────────────────────────────────────────── */
function renderHistory(rows) {
  S.hist = rows || S.hist || [];
  const sel = $("#f-status");
  if (!sel) return;
  const states = [...new Set(S.hist.map(r => r.state))];
  if (sel.options.length <= 1) sel.innerHTML = `<option value="">All statuses</option>` + states.map(s => `<option>${esc(s)}</option>`).join("");
  const fs = sel.value, fd = $("#f-dir").value, fx = ($("#f-symbol") || {}).value?.toUpperCase() || "";
  const tb = $("#hist-table tbody"); if (!tb) return;
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
