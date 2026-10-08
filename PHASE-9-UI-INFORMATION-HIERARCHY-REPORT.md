# Phase 9 — UI/UX Information-Hierarchy Restructure Report

## Overview

Phase 9 is a **presentation-layer-only** restructure. No trading methodology, OpportunityEngine semantics, causal logic, watcher behaviour, Gate A, or backend safety constraints were changed. `live_execution_enabled=false` and `order_send=0` / `order_check=0` remain enforced at the backend.

---

## Information Hierarchy (new)

```
HEADER  →  brand | mode-selector | health-chip | status-cluster | SYSTEM▾
SYSTEM DRAWER (hidden)  →  subsystem grid | market observer | weekly plan
─────────────────────────────────────────────────
WATCHLIST | CHART AREA                | SIGNALS PANEL
          | toolbar + quote-strip     | tabs: OPPS | ALERTS | EVENTS
          | gate-b-chip               |
          | chart canvas              |
          | CONTEXT DRAWER (hidden)   |
          |  SETUP | RISK | EXEC | LC |
─────────────────────────────────────────────────
FOOTER  →  LIVE EXECUTION DISABLED | HISTORY toggle
HISTORY SECTION (hidden)
```

**Tiers:**
1. **Primary** — chart, watchlist, compact Gate B chip, READY opportunity cards  
2. **Secondary** — context drawer (opens on Gate B ready or card click), alerts, events  
3. **Tertiary** — system drawer (diagnostics, build, market observer, weekly plan), history table

---

## Components Consolidated / Moved / Collapsed

### Removed / Replaced

| Old component | Action | New home |
|---|---|---|
| `#gateb-strip` (always-visible 3-row strip) | → compact one-liner chip | `#gateb-chip` in center column |
| `#panels` + 4 `.panel` bottom divs (SETUP / RISK / EXECUTION / LIFECYCLE) | → single tabbed context drawer, hidden by default | `#context-drawer` below chart |
| `#rightbar` MARKET OBSERVER panel | → sys-drawer secondary section | `#sys-drawer .sys-section` |
| `#rightbar` WEEKLY PLAN panel | → sys-drawer secondary section | `#sys-drawer .sys-section` |
| `#rightbar` OPPORTUNITIES panel | → Signals panel OPPORTUNITIES tab | `#sig-opportunities` |
| `#rightbar` ALERTS panel | → Signals panel ALERTS tab | `#sig-alerts` |
| `#rightbar` CAUSAL EVENTS panel | → Signals panel EVENTS tab | `#sig-events` |
| `#sys-strip` (always-visible subsystem row) | → SYSTEM drawer, opened on demand | `#sys-drawer` |
| `#build-chip` (inline in old sys-strip) | → inside sys-drawer header row | `#sys-drawer .sys-row` |
| 5 always-visible right panels | → 3-tab signals panel | `#rightbar #signals-panel` |

### New Components

| Component | Purpose |
|---|---|
| `#health-chip` | Single-token health indicator in header: `● DATA HEALTHY` / `⚠ DATA DEGRADED` / `⛔ UNAUTHORIZED` / `○ DISCONNECTED` |
| `#sys-drawer` | Hidden system drawer: full subsystem diagnostic + Market Observer + Weekly Plan |
| `#context-drawer` | Hidden tabbed drawer below chart: SETUP / RISK / EXECUTION / LIFECYCLE, opens on Gate B transition or card click |
| `#signals-panel` | Tabbed right panel: OPPORTUNITIES / ALERTS / EVENTS (replaces 5 always-visible panels) |
| `.sig-card` | Compact opportunity card: symbol, direction glyph, state badge, type, blocker, age — no raw IDs |
| `.sig-alert` | Compact alert row: kind, symbol, message, age |
| `.sym-state-col` | Per-symbol state indicator in watchlist: `●` READY / `◐` DEVELOPING / `—` none, with direction glyph |
| `.struct-wrap` / `#struct-panel` | Structure overlay dropdown: grouped behind collapsible, POI + setup default ON |
| `#sig-float-btn` | Mobile-only float button to toggle rightbar overlay |

### Preserved (unchanged IDs, just relocated)

`#s-engine`, `#s-mt5`, `#s-acct`, `#s-md`, `#s-risk`, `#s-exec`, `#build-chip`, `#observer-body`, `#plan-form`, `#plan-list`, `#plan-new`, `#p-symbol`, `#p-tf`, `#p-bias`, `#p-target`, `#p-thesis`, `#p-trigger`, `#p-confirm`, `#p-invalidate`, `#p-save`, `#setup-body`, `#risk-body`, `#exec-body`, `#lifecycle-body`, `#opp-body`, `#alerts-body`, `#causal-events`, `#causal-detail`, `#gateb-status`, `#gateb-detail`, `#gateb-review`, `#hist-table`, `#f-status`, `#f-dir`, `#f-symbol`

**Changed ID (breaking for legacy app.js):** `#opp-funnel` → `#opp-funnel-strip`

---

## app.js Changes

### Rendering changes
- `renderOpportunities()` — compact `.sig-card` cards grouped by READY / DEVELOPING / WATCHING; populates `S.symStates`; calls `buildSymbolList()` to refresh watchlist indicators; fix `#opp-funnel` → `#opp-funnel-strip`
- `renderAlerts()` — compact `.sig-alert` rows with `_alertAge()` relative time
- `renderReadiness()` — targets `#gateb-chip` (class swap) instead of old `#gateb-strip`; only calls `openContextDrawer("setup")` on first transition to READY
- `renderSetup()`, `renderRisk()`, `renderExec()`, `renderLifecycle()` — all target context-drawer panes (`#setup-body`, `#risk-body`, `#exec-body`, `#lifecycle-body`)
- `buildSymbolList()` — adds `.sym-state-col` with dot + direction glyph from `S.symStates`
- `renderStatus()` — populates `#health-chip` with derived health state; `#s-*` elements now in sys-drawer

### New helpers / functions
- `openContextDrawer(tab)` / `closeContextDrawer()` — show/hide `#context-drawer`; switch to named tab
- `switchCtxTab(tab)` — activates a context drawer tab
- `switchSigTab(tab)` — activates a signals panel tab
- `_alertAge(iso)` — compact relative timestamp ("5m", "1.2h")
- `let _lastGateBStatus` — transition detection guard (prevents reopening drawer on every poll)

### Wire changes
- `#tf-buttons` — event delegation (prevents broken TF buttons after innerHTML rebuild)
- `[data-layer]` checkboxes — initialised from `.checked` state (POI + setup default ON)
- `#struct-btn` / `#struct-panel` — click-outside-to-close dropdown
- `#sys-btn` / `#sys-drawer` — toggle with ▾/▴ indicator
- `.ctx-tab`, `#ctx-close` — context drawer tab/close
- `.sig-tab` — signals panel tab
- `#hist-toggle`, `#hist-close` — history section show/hide
- `#sig-float-btn` — mobile rightbar overlay toggle

---

## CSS Changes

All new style tokens introduced using the existing CSS variable palette (`--bg`, `--bg2`, `--bg3`, `--line`, `--txt`, `--dim`, `--faint`, `--bull`, `--bear`, `--acc`, `--warn`). No design tokens changed.

New style groups:
- `#health-chip`, `.health-ok/warn/err/unknown`
- `#sys-drawer`, `.sys-row`, `.sys-secondary`, `.sys-section`, `.sys-sec-h`
- `.sym-dot-ready/dev/none`, `.sym-dir-glyph`, `.sym-state-col`
- `.struct-wrap`, `#struct-btn`, `#struct-panel`
- `#gateb-chip`, `.gb-dot`, `.gb-sub`, `.gateb-waiting/ready/blocked`
- `#context-drawer`, `#ctx-header`, `#ctx-tabs`, `.ctx-tab`, `#ctx-close`, `.ctx-pane`
- `#signals-panel`, `#sig-tabs`, `.sig-tab`, `.tab-badge`, `.sig-pane`
- `#opp-funnel-strip`, `.funnel-stat`, `.funnel-stat-dim`, `.sig-group-h`
- `.sig-card`, `.sig-card-head/sym/dir/state/type/blocker/age`
- `.sig-alert`, `.alert-body/kind/sym/msg`, `.age`
- `@media (max-width: 960px)` — rightbar collapses to slide-in overlay
- `@media (max-width: 600px)` — sidebar collapses

---

## Backend Changes (non-UI, same session)

| File | Change | Reason |
|---|---|---|
| `hub.py` | `execution_ready_count` restored to registry-based (backward compat); `engine_execution_ready_count` added as new field | Fix 2 failing tests in `test_admission_semantics.py` |
| `opportunity/repository.py` | `threading.Lock()` → `threading.RLock()` | `@serialized_write` decorator + inner `with self._lock:` caused same-thread deadlock |
| `opportunity/repository.py` | Remove `@_serialized_read` from `_row_to_opp` (pure JSON parse, no SQL) | Called from within `@_serialized_read`-decorated `get()` — re-entrancy deadlock |
| `opportunity/repository.py` | Add `@_serialized_read` to `query()` | Concurrent `_conn.execute()` on shared connection without lock |
| `web/dbwrite.py` | `serialized_write` body now acquires `self._lock`; new `serialized_read` decorator | Per-connection serialization for concurrent access |

---

## Safety Invariants Verified

- `live_execution_enabled=false` — not changed
- `order_send`, `order_check`, `TRADE_ACTION` — not present in `app.js` or `index.html` (static assert in tests)
- `"BUY NOW"`, `"SELL NOW"`, `"TRADE NOW"` — not present in `index.html` (static assert in tests)
- `"GATE B"` and `"REVIEW SETUP"` — present in `index.html` (readiness guard preserved)
- `"WAITING FOR LIVE TICK"`, `"MARKET DATA REQUEST FAILED"`, `"MARKET DATA UNAVAILABLE"` — present in `app.js`
- Paper execute button gated by `S.risk.state === "OK"` and `st.display === "EXECUTION_READY"` (static assert in tests)
- 482 passed, 1 skipped (full suite with all Phase 9 changes)

---

*Generated: 2026-10-08*
