# UI-CONTEXT-CONSISTENCY-AUDIT

Dashboard symbol / quote / chart / setup context consistency and the
opportunity-vs-causal-setup relationship.

Repository: `tex-node/smc_engine` · Branch: `feature/gui-workstation`
Base (preserved): `b1196bd` (Phase 14C) · parent `ffe4e18`.
Status: **working tree only — not staged, not committed, not pushed.**

---

## 1. Root cause / confirmed explanation

The dashboard kept **one chart selection** (`S.symbol`) but rendered its
sub-views from **independent sources at independent times**, so the views could
disagree. Five concrete, code-level defects were confirmed:

1. **Chart header labelled from the selection, candles from the data.**
   `drawChart()` set `#chart-symbol` from `S.symbol` while the candles and the
   price tag came from `S.analysis`. Whenever `S.analysis` lagged `S.symbol`
   (a pending load, or a redraw triggered by resize/SSE), the header named one
   instrument while the candles/quote belonged to another.
2. **Stale live price retained on failure.** On an analysis error,
   `loadAnalysis` set `S.analysis = null` and re-rendered the quote, but never
   cleared `#chart-price` (`updatePriceTag` only ever wrote a value). The last
   successful symbol's price therefore persisted **as if live** — e.g. a
   XAUUSD `4168.998` tag left standing while the chart/quote showed EURAUD.
3. **`renderQuote()` was called with no argument** in `loadAnalysis`
   (`drawChart(); renderQuote();`), so the quote strip received `undefined` and
   always rendered its *no-data* branch regardless of the real quote.
4. **No stale-response guards** on `loadSetups`, `loadReadiness`,
   `loadCausalEvents`. A delayed response for a previously selected symbol could
   overwrite the newer selection's setup/readiness/history panels.
5. **Context conflation.** The OPPORTUNITIES panel is *global* (all symbols),
   but the context drawer always described the **chart's** causal candidates.
   Selecting a persisted `READY` opportunity did not surface that opportunity's
   own identity/lifecycle, so "READY opportunities" and "NO CAUSAL SETUP" looked
   contradictory.

### Proven vs explained
* **Proven by reproducible tests:** defects 1–5 and the chart↔opportunity
  context separation (see §5).
* **Explained, not byte-reproduced:** the exact screenshot composite
  (sidebar XAUUSD + header EURAUD + price 4168 + DATA UNAVAILABLE + setup
  XAUUSD) is the combination of defects 1–4 across rapid selection changes and a
  failed analysis. A byte-exact reconstruction of that live frame was not
  feasible headless; every mechanism that could produce it is fixed and tested.

---

## 2. Code-level evidence (files / functions)

| Location | Role |
|---|---|
| `src/smc_engine/web/static/app.js` `drawChart()` | labelled `#chart-symbol` from `S.symbol`; candles/price from `S.analysis` |
| `app.js` `loadAnalysis()` | called `renderQuote()` with no arg; did not clear the price on error; only guarded by `analysisSeq` |
| `app.js` `loadSetups()/loadReadiness()/loadCausalEvents()` | assigned symbol-scoped results with no symbol guard |
| `app.js` `renderOpportunities()` card click | set `S.symbol` and reopened the drawer, always showing chart candidates |
| `app.js` `renderSetup()/setupModel()` | always described `S.analysis.candidates` (chart context) |
| `app.js` `updatePriceTag()` | only writer of `#chart-price`; never cleared |
| `index.html` `#context-drawer` | had no explicit context descriptor |
| `src/smc_engine/web/hub.py:57` | `LIVE_EXECUTION_ENABLED = False` (unchanged) |
| `api.py` `/api/opportunities` | global (no symbol) → the panel is cross-symbol by design |

---

## 3. Chart context vs opportunity context (product semantics)

* **Chart context** — `S.symbol` + `S.tf`: market data, quote, candles, and the
  causal setup the authoritative engine detects for *that* instrument
  (D1→H4→M15). `NO CAUSAL SETUP DETECTED` here is a valid engine result.
* **Opportunity context** — `S.selectedOpp`: a specific **persisted** opportunity
  from the (global) OPPORTUNITIES panel, with its own symbol, direction, type,
  `state`/`label`, timestamps, blocker, `next_expected`, risk status and
  optional setup linkage.

These are deliberately separate layers. **A `READY` opportunity is not a causal
setup and never implies execution readiness.** `EXECUTION_READY` is emitted by
the engine only when an opportunity is promoted to a real `TradeSetup`. So a
persisted `READY` opportunity for XAUUSD/ETHUSD can correctly coexist with
`NO CAUSAL SETUP DETECTED` for the displayed chart instrument.

---

## 4. Changes made (smallest architectural fix)

### `app.js`
* **State:** added `symbolSeq` (selection generation), `chartState`
  (`loading|ready|empty|unavailable`), `selectedOpp` (opportunity context).
* **Selection helpers:** `selectChartSymbol()`, `selectTimeframe()` bump
  `symbolSeq`, drop stale `S.analysis`, clear the price, and clear the
  opportunity context. `selectOpportunity()`, `clearOpportunitySelection()`,
  `loadOpportunityDetail()` (uses `/api/opportunities/{id}`).
* **`drawChart()`** labels `#chart-symbol` from the **drawn data**
  (`(a ? a.symbol : S.symbol)`), shows the timeframe, flags a mid-flight switch
  (`loading …`), and renders distinct loading/unavailable/empty states.
* **`loadAnalysis()`** adds a `symbolSeq` guard, tracks `chartState`, clears the
  live price on failure, and calls `renderQuote(a)`.
* **`renderQuote(a = S.analysis, err)`** now defaults to the current analysis.
* **`loadSetups/loadReadiness/loadCausalEvents`** capture `symbolSeq` and discard
  stale-symbol responses.
* **`renderContext()` + `renderOpportunityContext()`** render an explicit
  CHART/OPPORTUNITY banner and the selected opportunity's identity + lifecycle;
  the SETUP and LIFECYCLE panes show opportunity context when one is selected.
* Opportunity card click now calls `selectOpportunity()` (cards carry
  `data-opp`); the chart-context "no setup" message names the chart symbol and
  states that a persisted READY opportunity is not a causal setup.
* Boot guarded and functions exported when running under a Node harness.

### `index.html` / `styles.css`
* Added `#ctx-context` banner element and `.ctx-context` styles.

### `tests/test_marketdata_resilience.py`
* Updated `test_stale_response_protection_in_loadAnalysis` to assert the guard
  *pattern* (intent preserved; the guard was strengthened, not weakened).

No SMC methodology, causal path, risk parameters, opportunity lifecycle
semantics, execution gates, Telegram behaviour, or `LIVE_EXECUTION_ENABLED`
were changed.

---

## 5. Before / after behaviour

| Aspect | Before | After |
|---|---|---|
| Chart header symbol | from `S.symbol` (could mismatch candles) | from the drawn data `a.symbol` |
| Stale live price on error | retained old price tag | cleared to `—` |
| Quote strip | always no-data (`renderQuote()` no-arg) | reflects `S.analysis` |
| Symbol switch race | old response could overwrite | stale responses discarded |
| Timeframe switch | old data could linger | old context invalidated |
| Drawer context | implicit (chart only) | explicit CHART vs OPPORTUNITY banner |
| Opportunity click | changed symbol, showed chart candidates only | shows the opportunity's identity + lifecycle |
| READY vs NO CAUSAL SETUP | looked contradictory | clarified as separate layers |

---

## 6. Regression-test evidence

New behavioural harness runs the **real** `app.js` state machine under a stubbed
DOM/transport (no browser, no network): `tests/frontend/context_consistency.test.js`
(exercised via `tests/test_ui_context_consistency.py` → `node`).

Harness scenarios (all pass): XAUUSD selection coherence; header labelled from
drawn data; late old response discarded; failure clears the live price; three
chart states distinct; opportunity identity+lifecycle shown; opportunity switch
replaces the previous; chart context shows NO CAUSAL SETUP with an explicit
label; **timeframe change invalidates an outstanding request; rapid multi-symbol
switching with out-of-order resolution; an SSE tick after a context change does
not restore the old symbol; failed-request cleanup (no stuck loading / stale
price).**

`tests/test_ui_context_consistency.py` — **13 passed**: the Node harness, static
structural guards (data-symbol label, `clearPriceTag`, `symbolSeq` guards,
distinct states, opportunity-context surface, time-frame invalidation, SSE does
not mutate selection, no broker primitives) and backend API contracts
(`/api/analysis` self-identifying, global `/api/opportunities`, detail matches
row, `/api/setups` symbol-scoped, READY ≠ causal setup).

Mapping to the required 12 checks: symbol/quote/label coherence ✓; no mixed
price-series/quote ✓; delayed-response protection ✓; timeframe invalidation ✓;
opportunity identity shown ✓; opportunity switch ✓; READY coexists with NO
CAUSAL SETUP ✓; setup panel never claims a setup from READY alone ✓; failed data
does not leave a live-looking quote ✓; distinct states ✓; SSE does not change the
selection unexpectedly ✓; existing suites green ✓.

### Full test results
```
pytest -q  →  584 passed, 1 skipped, 0 failed, 0 errored   (9m27s)
```
Baseline (Phase 14C `b1196bd`): 571 passed, 1 skipped. Delta **+13** (this suite).
No unrelated regressions; causal setup, opportunity lifecycle, conflict
arbitration, Telegram deduplication and execution-safety tests all pass.

No formatter/linter/type-checker is configured in the repository, so none was run.

---

## 7. Remaining limitations

* The frontend harness uses a **stubbed DOM**, not a real browser; true
  pixel/layout end-to-end coverage is not in the suite.
* The exact live screenshot frame was **explained but not byte-reproduced**.
* `MARKET_UPDATE` refreshes the toolbar price only for the current symbol; the
  quote strip updates on the analysis poll (20 s) rather than on every tick.
* Opportunity cards are refreshed globally on SSE; the selected opportunity's
  identity persists across chart-context changes only until the user changes the
  chart symbol/timeframe (by design).

---

## 8. Safety confirmation

* `LIVE_EXECUTION_ENABLED = False` (unchanged).
* No broker/execution primitives in the changed frontend files
  (`order_send` / `TRADE_ACTION` absent); no order was sent.
* No secrets/tokens in the diff; `telegram.txt` remains ignored/untracked and
  nothing was staged.
* No SMC strategy, causal, risk, lifecycle, gate, or Telegram behaviour changed.

---

## 9. Current state

* Branch `feature/gui-workstation`, **HEAD `b1196bd`** (Phase 14C, parent
  `ffe4e18`), ahead 1 of `origin`.
* Phase 14C work (commit `b1196bd`, tests, reports) **preserved and unchanged**.
* Changed (unstaged): `src/smc_engine/web/static/app.js`,
  `web/static/index.html`, `web/static/styles.css`,
  `tests/test_marketdata_resilience.py`.
* New (untracked): `tests/test_ui_context_consistency.py`,
  `tests/frontend/context_consistency.test.js`, `UI-CONTEXT-CONSISTENCY-AUDIT.md`.
* Unrelated untracked audit reports untouched.

**Verdict:** the identified symbol/quote/label/price mismatch, the stale-response
and stale-price defects, and the opportunity-inspection flow are fixed and
covered by reproducible tests (584 passed / 1 skipped). Not committed or pushed
pending review of this diff and its test results.

---

## 10. Independent review (Phase 15)

Independent code-level review of the diff against the 19 required invariants.

### Confirmed defects (fixed in this diff)
1. Chart header labelled from the selection while candles/quote came from the
   analysis (mismatch under lag).
2. Failed analysis retained the previous instrument's `#chart-price` as a live
   value.
3. `renderQuote()` invoked with no argument in `loadAnalysis` → quote strip
   never showed the real quote.
4. No stale-symbol guards on `loadSetups`/`loadReadiness`/`loadCausalEvents`.
5. Opportunity vs chart context conflated (selected opportunity never rendered).

### Resolved concerns (reviewed, no change needed)
* Stale-response safety is correctly scoped: `analysisSeq` (identical-context
  ordering) **and** `symbolSeq` (context identity) are both checked on the
  success and failure paths of `loadAnalysis`.
* SSE cannot change the selection: no handler assigns `S.symbol`; `MARKET_UPDATE`
  is guarded by `p.symbol === S.symbol`; `SETUP_*` refreshes are seq-guarded.
* A successful HTTP response is not treated as fresh data solely because it
  returned — freshness comes from the backend `quote.market_data`/`tick_time`.
* No front-end path promotes a persisted opportunity to `EXECUTION_READY`; the
  opportunity pane states explicitly that a READY opportunity is not a setup.
* `renderRisk()` (async, symbol-scoped) lacks its own seq guard, but `S.risk`
  only gates the paper button by `S.risk.for === c.id` (setup id), so a stale
  risk response cannot enable execution. Noted, not a defect.

### Remaining limitations
* No real-browser (DOM/layout) test: none of the project's dependencies provide
  a headless browser, and introducing one was judged unjustified. The Node
  harness is the practical maximum.
* The exact live screenshot frame remains unverified in a real browser; every
  mechanism that could produce it is fixed and behaviourally tested.
* Quote strip refreshes on the analysis poll; only the toolbar price is ticked.
* `S.selectedOpp` clears when the chart symbol/timeframe changes (by design).

### Test-quality assessment
The Node harness `require`s the production `app.js` and drives its real exported
functions, asserting observable DOM (`textContent`/`innerHTML`) and `S` state
with a stubbed DOM/transport — it is behavioural, not structural. Stubs are
limited to `document`/`window`/`EventSource`/`fetch`/timers; the SSE path is now
exercised by invoking the real registered handlers. Gaps: no layout/paint
coverage and no real network. The Python file's static guards are structural by
design (repo convention) and are backed by the behavioural harness.

### Working-tree scope (Phase 15)
* Modified (unstaged): `src/smc_engine/web/static/app.js`,
  `src/smc_engine/web/static/index.html`, `src/smc_engine/web/static/styles.css`,
  `tests/test_marketdata_resilience.py`.
* New (untracked): `tests/frontend/context_consistency.test.js`,
  `tests/test_ui_context_consistency.py`, `UI-CONTEXT-CONSISTENCY-AUDIT.md`.
* HEAD unchanged at `b1196bd` (Phase 14C); nothing staged; not pushed.

### Test results (Phase 15)
```
focused (ui_context + marketdata + semantics)  →  30 passed
pytest -q                                       →  584 passed, 1 skipped, 0 failed, 0 errored
```

**Final recommendation: APPROVE WITH LIMITATIONS.** The five confirmed defects are
fixed and covered by reproducible behavioural tests; Phase 14C is intact and
`LIVE_EXECUTION_ENABLED=False`. The limitation is the absence of a real-browser
end-to-end test (and therefore an unverified live-browser screenshot
reproduction), plus the minor `renderRisk` guard observation. Files that should
form the eventual UI fix commit:
`src/smc_engine/web/static/app.js`,
`src/smc_engine/web/static/index.html`,
`src/smc_engine/web/static/styles.css`,
`tests/test_marketdata_resilience.py`,
`tests/test_ui_context_consistency.py`,
`tests/frontend/context_consistency.test.js`,
`UI-CONTEXT-CONSISTENCY-AUDIT.md`.
