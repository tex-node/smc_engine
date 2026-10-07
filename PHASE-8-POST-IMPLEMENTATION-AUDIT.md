# PHASE-8-POST-IMPLEMENTATION-AUDIT

Read-only audit of the Opportunity Layer. No fixes implemented. Evidence was
gathered live (HTTP GET only) and by code inspection; no broker primitives were
invoked (`order_send=0`, `order_check=0`, book pending/positions = 0).

---

## 1. Executive verdict

**The Opportunity Layer is structurally sound but not yet correct in its live
behaviour.** The architecture, persistence, state machine, background
discovery, alert de-duplication and safety boundary are all verified. However
three defects materially affect opportunity validity and coverage:

* **P1-A (causal pairing):** 42 of 47 live opportunities (89%) attach a POI
  created far outside the configured `csd_to_poi` window (up to ~1967 h / 82
  days) to an old sweep/CSD anchor. The counts (24 READY / 23 ENTRY_TRIGGERED)
  are therefore inflated by causally meaningless pairings.
* **P1-B (persistence):** `sqlite3.OperationalError: database is locked` caused
  20/24 symbols to fail in the sampled scan (universe coverage collapsed to
  4/24 = 17% at that moment; 8/24 earlier). Root cause is multi-connection
  write contention on the shared GUI database.
* **P1-C (labelling):** `ENTRY_TRIGGERED` renders as **"EXECUTION READY"** in
  the UI and emits an `EXECUTION_READY` SSE/alert kind (23 persisted alerts)
  although no TradeSetup and no risk approval exist.

Positive verification: 23/23 ENTRY_TRIGGERED entry touches were independently
recomputed from live bars and confirmed; evidence chains are complete and IDM is
never bypassed on the PULLBACK pathway; the background watcher provably runs
without any UI interaction; discovery is 100% non-executing.

**Verdict: Phase 8 is NOT yet safe to build further opportunity-generation
logic on.** Fix P1-A/P1-B/P1-C first.

---

## 2. Architecture audit

`MARKET DATA → CAUSAL ENGINE → OPPORTUNITY ENGINE → TradeSetup → RISK → EXECUTION`
is implemented as specified. The causal engine remains authoritative for
structural truth; `TradeSetup` creation is still owned exclusively by the causal
path (the opportunity engine only links, `CONVERTED_TO_SETUP`). Separation of
*CAUSAL TRUTH* vs *OPPORTUNITY STATE* holds: opportunities persist while no
setup exists.

Boundary of authority per element:

| Element | Source | Classification |
|---|---|---|
| D1 POI | `poi.build_d1_pois` / `active_unmitigated_pois` | AUTHORITATIVE (reused verbatim) |
| H4 sweep | `structure.detect_sweeps` | AUTHORITATIVE |
| H4 CSD | `structure.confirm_csd` | AUTHORITATIVE |
| M15 OB | `execution_structure.find_order_blocks` | AUTHORITATIVE |
| FVG | `fvg.detect_fvgs` | AUTHORITATIVE (id made timestamp-derived) |
| IDM | `execution_structure.find_inducements` | AUTHORITATIVE |
| IRL | not used by the opportunity layer | AUTHORITATIVE (setup engine only) |
| Structural invalidation | opportunity layer: opposing CSD only | **OPPORTUNITY-SPECIFIC (partial)** — protected-level breach not checked (P2-D) |
| Entry touch | opportunity layer policy (zone overlap after POI formation) | OPPORTUNITY-SPECIFIC (ADAPT) |
| Windows/TTL/pathways/ranking | opportunity layer | OPPORTUNITY-SPECIFIC (ADOPT/ADAPT) |

---

## 3. ENTRY_TRIGGERED semantics

Single creation path: `OpportunityEngine._check_entry()` → `_terminate(..., St.ENTRY_TRIGGERED, ENTRY_PASSED)`.
Required evidence at that point: opportunity had reached `READY_FOR_MITIGATION`
(sweep+CSD+POI+IDM for PULLBACK), a selected POI record with a zone, and a bar
**strictly after** `max(csd/bos anchor, poi.created_time + 15 min)` whose range
overlaps the POI zone (`low ≤ zone_high and high ≥ zone_low`).

Answers:

* **Without a valid TradeSetup?** Yes — by design. ENTRY_TRIGGERED is an
  opportunity-side "entry touch" observation; all 23 live instances have
  `setup_id=''`. It is terminal for the opportunity.
* **Bypass causal requirements?** No for the *elements* (sweep/CSD/POI/IDM all
  come from the authoritative engine). **Yes for the *timing*:** the anchor and
  the POI are not time-bounded together, so an ancient CSD can be paired with a
  recent POI (P1-A).
* **Bypass IDM?** Not on PULLBACK (23/23 have `idm_reference`). AGGRESSIVE and
  CONTINUATION intentionally skip the IDM wait (documented ADAPT).
* **Bypass structural invalidation?** Yes, partially: only opposing CSD
  invalidates; a protected-level breach does not terminate an opportunity
  (P2-D). The setup lifecycle still invalidates correctly.
* **Bypass risk / portfolio policy?** Yes, by design — the opportunity layer
  never consults risk. Risk remains strictly downstream, so an ENTRY_TRIGGERED
  opportunity can never authorize anything.
* **Incorrectly interpreted by the UI as execution-ready?** **Yes** — label
  `"EXECUTION READY"` + alert kind `EXECUTION_READY` (P1-C).

State transition diagram:

```
IDLE ──sweep armed──▶ OPPORTUNITY_ARMED ──no CSD yet──▶ WAITING_FOR_CONFIRMATION
  │                          │                                │
  │                          └──CSD confirmed─────────────────┘
  ▼                                                     ▼
STRUCTURAL_CONTEXT                            WAITING_FOR_POI ──POI found──▶ WAITING_FOR_IDM
                                                        │                        │
                                                        └──no POI (TTL)──▶ EXPIRED │
                                                                                 ▼
                    ┌────────────── opposing CSD ──▶ INVALIDATED        READY_FOR_MITIGATION
                    │                                                    │            │
                    │                              POI consumed (re-plan) ┘            │
                    │                                                                 ▼
                    │                                                    ENTRY_TRIGGERED (terminal)
                    └── TTL ──▶ EXPIRED                                    │
                                                                           └── CONVERTED_TO_SETUP (link only)
```

---

## 4. 47-opportunity sample analysis

Distribution: `REVERSAL/ENTRY_TRIGGERED/PULLBACK = 23`, `REVERSAL/READY_FOR_MITIGATION/PULLBACK = 19`,
`CONTINUATION/READY_FOR_MITIGATION/CONTINUATION = 5`.

Evidence completeness: ENTRY_TRIGGERED (23) → sweep 23, CSD 23, POI 23, IDM 23,
BOS 0, setup 0. READY (24) → POI 24, IDM 19 (the 5 without IDM are the
CONTINUATION ones), setup 2.

Representative samples (verbatim from `/api/opportunities`):

* `OPP-REVERSAL-ETHUSD-SWEEP-1789056000000000000-BULLISH` — ENTRY_TRIGGERED,
  sweep 2026-09-10 16:00 (SELL_SIDE, 2442.21→2431.21), CSD 2026-09-11 12:00
  (BULLISH @2523.12), POI `OB-M15-1791270900000000000-BULLISH` created
  **2026-10-06 07:15** [2695.69, 2698.42], IDM
  `IDM-OB-M15-1791270900…-1791279900000000000`, setup —, risk —,
  blocker ENTRY_PASSED. → **anchor-to-POI gap ≈ 624 h (26 days).**
* `OPP-REVERSAL-ETHUSD-SWEEP-1786896000000000000-BEARISH` — READY, sweep
  2026-08-16 16:00, CSD 2026-08-16 20:00, POI created **2026-10-07 17:00**
  → gap ≈ 1245 h (52 days).
* `OPP-CONTINUATION-CHFJPY-BOS-1791374400000000000-BEARISH` — READY, BOS
  2026-10-07 12:00 @189.887, POI `FVG-S-1791387900000000000` created
  2026-10-07 15:45 → **gap 3.75 h (valid same-session pairing).**
* Converted: `AUDCAD` ×2 → `SETUP-AUDCAD-…-OB-M15-1791375300000000000-BULLISH`
  and `…-1791383400000000000-BULLISH`, lifecycle `SETUP_REGISTERED`.

**ENTRY_TRIGGERED verification:** all 23 were independently recomputed from live
M15 bars (zone overlap after the anchor) → **verified 23, unverified 0**. The
*label* is mechanically correct; the *causal pairing* is not (see P1-A).

POI candidate tracking works: rejections recorded in volume —
`POI_DIRECTION_INVALID 2760, POI_CONSUMED 951, POI_EXPIRED 568, POI_TOO_SMALL 317,
POI_STRUCTURALLY_INVALID 256`; no candidate silently discarded.

---

## 5. Two-setup conversion analysis (funnel audit)

```
47 opportunities
 ├─ 2  CONVERTED_TO_SETUP        (AUDCAD ×2, linked to causal setups)
 ├─ 23 ENTRY_CONDITION_ONLY      (entry touched; causal analyzer emitted no candidate → no setup)
 ├─ 17 MISSING_CAUSAL_REQUIREMENT (READY reversal: POI+IDM present, analyzer produced no candidate)
 └─ 5  CONTINUATION_NOT_IN_CAUSAL_SPEC (continuation stream has no causal-engine counterpart)
```

No generic "rejected" bucket; every non-conversion has a specific reason. Zero
`RISK_REJECTED` / `PORTFOLIO_RISK_REJECTED` / `TARGET_INVALID` / `INVALIDATION`
in the current live set (risk is never reached without a setup).
**Caveat:** under P1-A the 23 `ENTRY_CONDITION_ONLY` and most of the 17
`MISSING_CAUSAL_REQUIREMENT` entries are stale-anchor artifacts, so the
"recovered opportunity" claim must be re-measured after the P1-A fix.

---

## 6. Symbol failures (AUDIT 4)

| SYMBOL | STAGE | ERROR | ROOT CAUSE | RECOVERABLE | RECOMMENDED FIX | CURRENT IMPACT |
|---|---|---|---|---|---|---|
| AUDCAD, AUDUSD, CHFJPY, ETHUSD, EURCHF, EURGBP, EURJPY, EURUSD, GBPAUD, GBPCAD, GBPCHF, GBPJPY, GBPUSD, NZDUSD, USDCAD, USDCHF, USDJPY, XAGUSD, XAUUSD, XAUUSD247 (20) | opportunity persistence (`repo.upsert`/`record_event_once`) inside `observe` | `sqlite3.OperationalError: database is locked` | SQLite write contention: WebStore + SetupEventHistory + OpportunityRepository + SetupStore all write the same GUI file from poller + request threads; single-writer WAL + 10 s busy timeout exceeded during heavy scans | Yes | Serialize writes (shared writer lock / single writer thread / batch commits), raise `busy_timeout`, or give the opportunity tables their own DB with one writer | Universe coverage collapses (4/24 = 17% at sample; 8/24 earlier); opportunity updates and alerts lost for affected symbols |

Classification: **DATABASE** (all 20). No DATA / BROKER / HISTORY / TIMEFRAME /
CAUSAL / NETWORK / UI failures were observed.

Coverage metrics at sample time: **universe coverage 4/24 = 17%**
(successful/eligible); **opportunity coverage** — cumulative symbols with
opportunities: 8 (AUDCAD, AUDJPY, AUDUSD, BTCUSD, CADJPY, CHFJPY, ETHUSD,
EURAUD), i.e. ≥8/8 of the symbols that ever succeeded carried opportunities
(100% of successfully-scanned symbols), but the *current-scan* ratio is 0/4
because the latest scan skipped all (closed-M15 gate) and the sampled failures
dominate.

Diagnostics weakness (P2-E): `last_error` persists across skipped scans and a
silent empty scan (`eligible_symbols() → []`) is indistinguishable from
"all skipped" — `last_scan` showed `scanned=0, failed=0, skipped=0` with no
scan-level error field.

---

## 7. Background discovery proof

Call graph (UI-independent):

```
start_poller() [15 s daemon thread]
  → scan_universe_once()
      → eligible_symbols()            # source.symbols() — broker universe
      → per symbol: _last_closed_m15()# closed-M15 gate
      → _causal_view()                # causal primitives (authoritative)
      → OpportunityEngine.observe()   # state machine + persistence
      → _dispatch_opportunity_events()# web.add_alert + SSE publish
      → expire_cycle(now=data_now)    # TTL sweep on closed-data time
```

None of the following appear anywhere in this path: selected symbol, tab, chart,
browser focus, `/api/analysis`, `loadAnalysis()`, frontend polling. `watchlist`
is not consulted by the watcher.

Runtime proof (no UI interaction between samples): `last_scan_time` advanced
`21:03:26 → 21:03:56` while `watcher_running=True`, `universe=24`; the second
scan reported `symbols_skipped=24` (closed-M15 gate) — the loop is driven purely
by the backend cadence. Discovery for symbols never selected in the UI
(AUDCAD, AUDJPY, BTCUSD, ETHUSD, CADJPY, CHFJPY…) is itself proof: those
symbols had no `/api/analysis` traffic. Unit proof:
`test_discovery_without_ui_selection` (watchlist = [EURAUD] → GBPUSD opportunity
persisted + alerted, watchlist unchanged, zero broker sends).

---

## 8. Causal integrity mapping

| Transition | Relies on | Classification |
|---|---|---|
| ARM (reversal) | `detect_sweeps` (H4, authoritative) | ADOPT |
| ARM (continuation) | `detect_structure_breaks` BOS (H4, authoritative) | ADOPT |
| WAITING_FOR_POI ← CSD | `confirm_csd` (authoritative, 6-bar window) | ADOPT |
| POI selection | D1 POI / M15 OB / FVG primitives + opportunity ranking | ADAPT (ranking/rejections are opportunity-specific; geometry authoritative) |
| IDM confirmation | `find_inducements` (authoritative) | ADOPT |
| READY | conjunction of the above | ADAPT (opportunity-side readiness) |
| ENTRY_TRIGGERED | opportunity zone-overlap policy | ADAPT |
| INVALIDATED (opposing CSD) | `confirm_csd` output | ADOPT |
| INVALIDATED (protected breach) | **not implemented** | REJECT/gap (P2-D) |
| IRL | not used | ADOPT (setup engine owns it) |
| Windows/TTL | opportunity config | ADOPT (new, centralized) |

No causal definition was silently redefined. The one *silent* deviation is the
missing time-bound coupling between anchor and POI (P1-A) — an ADAPT that must
be made explicit and enforced.

---

## 9. Look-ahead audit

Implementation inspection: `evaluator._truncate()` filters D1/H4/M15 to
`time ≤ as_of` before any primitive runs; `build_view` derives `m15_last_time`
and `m15_atr` from the truncated frame; `poi_candidates` ages against
`view.m15_last_time`; `_check_entry` filters `m15_frame` to bars `> anchor`
(the frame is already truncated); `observe` uses `view.m15_last_time` as `now`;
the watcher's TTL sweep uses the latest closed-data time, not the wall clock.
No path reads a candle after `as_of`, no future POI/mitigation/target/
invalidation state is consulted. Verified by
`test_no_lookahead_truncation_equivalence` (identical evidence with truncated
vs full input at the same `as_of`) and `test_replay_deterministic`.
Residual (P3): state-history/event *timestamps* use wall clock (`_utcnow()`) —
values are deterministic, timestamps are not.

---

## 10. Lifecycle audit

Every transition funnels through `_transition`/`_terminate`, each of which
writes `repo.record_state_change(opportunity_id, from, to, reason, detail)` with
a timestamp and persists the opportunity (`updated_at`, `reason`, `blocker`,
`next_expected`). Cancellation paths present: `EXPIRED` (TTL/sweep-window),
`INVALIDATED` (opposing CSD), `ENTRY_TRIGGERED` (entry touch), `TERMINAL`
(never re-enters; duplicate sweeps/BOS are deduplicated by canonical key).
`DUPLICATE` is handled by identity (`UNIQUE(canonical_key)`), and terminal
states cannot resurrect (state-machine table + `test_expired_opportunity_never_resurrects`,
`test_terminal_historical_setup_does_not_reactivate_opportunity`).
Sample `history` returned by `/api/opportunities/{id}` contains
`WAITING_FOR_POI ← armed ← SWEEP`, `READY_FOR_MITIGATION`, `ENTRY_TRIGGERED`.

---

## 11. Alert audit

Persisted alert kinds: `OPPORTUNITY_CREATED 47`, `POI_FOUND 47`, `READY 47`,
`IDM_CONFIRMED 42`, `EXECUTION_READY 23`, `CONVERTED_TO_SETUP 3`;
engine `alerts_emitted=209` equals the opportunity-kind alert total (SSE and
persisted agree). Idempotency: `UNIQUE(opportunity_id, transition, evidence_key)`;
`test_repeated_scans_are_idempotent_and_alert_once` proves alert totals are
constant across repeat scans, and restart recovery does not resend historical
alerts (reconstructed rows are not re-emitted). Findings: (a) the
`EXECUTION_READY` kind is misleading (P1-C); (b) 3 conversion alerts vs 2
currently linked setups (P2-F).

---

## 12. UI semantic audit

Panel labels: WATCHING / ARMED / DEVELOPING / READY / **EXECUTION READY** /
INVALIDATED / EXPIRED. Distinctness of OPPORTUNITY vs SETUP is mostly preserved
(the panel shows `SETUP: —` and `RISK: pending`, and the setup card + PAPER
button are driven by `/api/setups`/`/api/risk`, never by opportunities).
**Defect:** an opportunity that merely touched its POI is rendered as
`EXECUTION READY` (and alerts as `EXECUTION_READY`) — violating the rule that an
ENTRY_TRIGGERED opportunity must not look like an approved trade (P1-C).
Recommended: label `ENTRY TOUCHED (no setup)`; reserve `EXECUTION READY` for
setups with a lifecycle state.

---

## 13. Before/after funnel (101-sample replay, same fixture)

```
OLD ENGINE
  structural candidates:        0
  execution-ready observations: 0
  samples with any setup:       0 / 101

NEW ENGINE
  structural contexts:          detected (sweep/CSD present in the window)
  opportunities armed:          4
  CSD progression:              reached
  POI:                          selected (OB-M15-…)
  IDM:                          confirmed
  READY:                        1 (observed 18 samples)
  ENTRY_TRIGGERED:              1 (on mitigation touch)
  TradeSetup:                   0 (causal analyzer emitted no candidate)
  execution-ready:              0
  expired:                      3
  invalidated:                  0
  cycles with a developing opportunity while the OLD engine saw NO setup: 57
```

**Why opportunities were recovered:** the old architecture required the whole
D1→H4→M15 chain to resolve inside a single scan; the sweep/CSD evidence had
aged out of the analyzer's window before the POI/IDM formed, so the setup was
lost. The opportunity layer persists each qualifying event and advances the
state machine across cycles, so the developing trade stays visible. This is
genuine recovery **in the fixture** (anchor and POI are within the configured
windows). The live 47 counts are NOT yet valid evidence of recovery because of
P1-A (stale anchors). Success must be re-measured post-fix with the same funnel.

---

## 14. Safety verification

* `LIVE_EXECUTION_ENABLED = False`; `POST /api/live/x → 403`.
* Static: `order_send` / `order_check` / `TRADE_ACTION*` / `MetaTrader5` /
  `risk_engine` absent from `src/smc_engine/opportunity/*` (grep + test).
* Runtime: broker book read-only probe → `pending=0 positions=0`; discovery
  produces zero orders (`FakeDemoSource.sent == []` in tests).
* Risk remains downstream: the opportunity layer never calls RiskEngine or
  ExecutionPolicy; `paper_place` still requires `EXECUTION_READY` setup state,
  the demo gate, `_paper_gate`, risk validation and the portfolio budget.
* Gate A unchanged; Gate B remains **WAITING FOR REAL CAUSAL SETUP** (no setup
  manufactured).

---

## 15. Defects found

| ID | Defect | Evidence |
|---|---|---|
| P1-A | Stale anchor pairing: POI paired with an anchor outside the configured windows | 42/47 opportunities: POI created >24 h after anchor (up to ~1967 h); 36/47 sweep→CSD gap >6 h; code: `_discover_reversal` bypasses the age guard when a CSD exists, and `poi_candidates(created_after=csd_time)` has no upper bound |
| P1-B | `database is locked` → symbol coverage collapse | 20/24 symbols failed in a sampled scan; coverage 4/24 = 17% |
| P1-C | ENTRY_TRIGGERED labelled "EXECUTION READY" + `EXECUTION_READY` alert kind | `models.STATE_LABEL`; 23 persisted `EXECUTION_READY` alerts |
| P2-D | No protected-level-breach invalidation in the opportunity layer | only opposing CSD invalidates |
| P2-E | Diagnostics: stale `last_error`, silent empty scan indistinguishable from all-skipped | `last_scan` 0/0/0 with no scan-level error field |
| P2-F | 3 `CONVERTED_TO_SETUP` alerts vs 2 linked setups | alert/linkage mismatch |
| P3-G | State-history/event timestamps use wall clock | `_utcnow()` in `record_state_change`/`record_event_once` |

## 16. Severity

P1-A **P1** · P1-B **P1** · P1-C **P1** · P2-D **P2** · P2-E **P2** ·
P2-F **P2** · P3-G **P3**.

## 17. Recommended fixes (NOT implemented in this phase)

1. **P1-A** — enforce windows as hard bounds in discovery/advance: arm only if
   `sweep_age ≤ sweep_to_csd` OR a CSD within it exists; select a POI only if
   `poi.created_time − anchor ≤ csd_to_poi_bars`; do not refresh the READY TTL
   for stale chains; add `BlockReason.STALE_ANCHOR` and tests asserting that
   ancient sweeps/CSDs never yield READY/ENTRY_TRIGGERED.
2. **P1-B** — serialize database writes (single writer lock or a dedicated
   writer thread), raise `busy_timeout`, batch commits per scan, or move the
   opportunity tables to their own DB with one writer; add a regression test
   that runs concurrent scans + HTTP reads and asserts zero lock errors.
3. **P1-C** — relabel `ENTRY_TRIGGERED` (e.g., "ENTRY TOUCHED (no setup)") and
   emit `ENTRY_TRIGGERED`/`OPPORTUNITY_ENTRY_TOUCHED` instead of
   `EXECUTION_READY`; keep `EXECUTION_READY` for setups only; UI test asserting
   an opportunity can never render the setup/execution label.
4. **P2-D** — add protected-level-breach invalidation using the existing
   structural evidence (`PROTECTED_LEVEL_BREACHED`) as an explicit ADAPT.
5. **P2-E** — clear `last_error` on success, add `symbols_eligible` and a
   scan-level `scan_error` to diagnostics.
6. **P2-F** — reconcile conversion alerts with the current linkage.
7. **P3-G** — use the data timestamp for state-history rows during replay.

After P1-A/B/C are fixed, re-run the 101-sample funnel and the live coverage
audit before adding further opportunity-generation logic.
