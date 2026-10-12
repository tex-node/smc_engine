# SMC ENGINE — AGENT HANDOVER NOTE

_Authored 2026-10-12. Read this fully before acting. All facts below were verified in-session; re-verify anything you depend on (state drifts — the production server scans and writes continuously)._

---

## 1. What this project is

A **Smart-Money-Concepts (SMC) market-analysis engine** with three layers:

1. **Causal/strategy layer** — reads live MT5 market data, detects structure, sweeps, CSD/POI/IDM, and produces **TradeSetups** (observation only).
2. **Opportunity layer** — an explicit state machine that tracks developing opportunities across polling cycles, persists them, arbitrates conflicts, and emits deduplicated lifecycle events.
3. **Workstation/API + notifications** — a FastAPI GUI (`127.0.0.1:8765`), SQLite persistence, and outbound **Telegram** notifications (durable outbox).

It is **observation-only**. Live trading is disabled by a hard compile-time gate. There is a demo execution harness but no live authorization.

---

## 2. Repository, branch, commits

| item | value |
|---|---|
| Path | `C:\smc_engine` |
| Branch | `feature/gui-workstation` |
| Remote | `https://github.com/tex-node/smc_engine.git` (GitHub account `tex-node`) |
| HEAD | `78e5169afbcb658938c0eab0251113b3582bd74b` (docs: 16H.7–16H.9 evidence) |
| Deployed prod revision | `826e466693c3ed3b3cb34e846bba19d7e17fef5d` |
| CI | GitHub Actions `test` (`.github/workflows/test.yml`) |

Recent history:
```
78e5169 docs(phase-16h): record 16H.7-16H.9 operational evidence
826e466 feat(telegram): durable notification outbox with failure-injection tests
af5c4fe fix: add auditable opportunity quarantine safeguards
4682f63 fix(opportunities): enforce identity and terminal-state invariants
09c7ce2 fix(recovery): use per-symbol market time for expiry
```

**Note:** the docs commit `78e5169` is newer than the deployed `826e466`; the running server is on `826e466`. Deploying `78e5169` is a no-op for runtime (docs only) but `stale_code` will report `true` until restarted.

---

## 3. Environment (Windows)

- **Shell:** PowerShell 5.1 (this tool's shell). Python venv: `C:\smc_engine\.venv\Scripts\python.exe`.
- **Test command:** `.venv\Scripts\python.exe -m pytest -q` (run with `workdir=C:\smc_engine`).
- **MT5:** MetaTrader 5 running (multiple `terminal64.exe`). Demo account attached to a broker demo server (login / server / currency / equity redacted for this document).
- **`Varis_SMC`**: an MQL5 EA in the MT5 `MQL5\Experts` folder that shares the same Telegram bot/chat. **Do not modify, disable, or delete it, and do not modify MT5.**
- **Credentials:**
  - MT5: DPAPI-protected blob under `%APPDATA%\SMC_ENGINE\` (CurrentUser scope, engine-specific entropy; single-use provisioning; not committed).
  - Telegram: `C:\smc_engine\telegram.txt` (bot token + chat id; **untracked/gitignored**). Never print tokens/chat ids.
- **Databases** (`SMC_GUI_DB_DIR` unset → relative `./` from the launcher's `cd /d C:\smc_engine`):
  - `C:\smc_engine\smc_engine_gui.sqlite3` — GUI: opportunities, events, history, alerts, funnel, quarantine, notification outbox.
  - `C:\smc_engine\smc_engine_state.sqlite3` — setups + lifecycle events.
- **Launcher / stop:**
  - `start_workstation.bat` → `tools/load_mt5_credentials.ps1` (starts uvicorn in a titled cmd console, opens the browser).
  - `stop_workstation.bat` → `taskkill /FI "WINDOWTITLE eq SMC Engine Workstation Server*" /T`. **In practice this matched nothing and did not stop the server.** Escalate: find the launcher `cmd.exe` (parent of the `.venv` python) and `taskkill /PID <cmdPid> /T /F` after verifying ownership. Tree shape: `cmd.exe (launcher) → .venv python → system python (uvicorn listener)`.

---

## 4. Architecture / module map

```
src/smc_engine/
  causal.py, structure.py, poi.py, lifecycle.py, setup.py, strategy.py, models.py, market.py
  risk.py                 # RiskEngine: broker-aware sizing/validation
  execution_policy.py     # ExecutionPolicy: deterministic normalize/build order contract
  execution.py            # MT5ExecutionAdapter (broker boundary)
  order_guard.py          # MT5OrderGuard: idempotency / live invalidation
  execution_structure.py
  dry_run.py              # DryRunEngine (simulation)
  telegram.py             # TelegramNotifier (optional durable outbox)
  notification_ledger.py  # NotificationLedger (durable notification_outbox)
  opportunity/
    engine.py             # OpportunityEngine (observe/expire/discover/advance/promote)
    repository.py         # OpportunityRepository (SQLite; identity/terminal/quarantine invariants)
    models.py             # Opportunity, canonical_opportunity_key, opportunity_id_from_key, states
    evaluator.py          # CausalView, poi_candidates
    state_machine.py      # ALLOWED transitions, TERMINAL set
    arbiter.py            # OpportunityArbiter (conflict resolution)
  web/
    hub.py                # EngineHub (scan loop, dispatch, runtime, LIVE_EXECUTION_ENABLED gate)
    api.py                # FastAPI routes (build_hub → db_dir)
    runtime.py            # fingerprint (loaded_git_commit / stale_code)
    dbwrite.py            # shared writer lock (serialized_write/read), counters
    history.py, static/   # UI
tools/
  phase16e_identity_assessment.py        # read-only identity/lifecycle assessment
  phase16g_quarantine_opportunities.py   # dry-run-first quarantine utility
tests/  (pytest; see §8)
```

---

## 5. Data model (SQLite, `smc_engine_gui.sqlite3`)

| table | purpose |
|---|---|
| `opportunities` | one row per opportunity. PK `opportunity_id`; **`canonical_key` UNIQUE** (authoritative identity); `state`; `payload_json` (full serialized `Opportunity`). |
| `opportunity_state_history` | append-only state transitions. |
| `opportunity_events` | lifecycle events; `UNIQUE(opportunity_id, transition, evidence_key)` (durable dedup). |
| `opportunity_quarantine` | `opportunity_id PK, reason, at, evidence_json` — quarantined ids. |
| `notification_outbox` | durable Telegram outbox (see §7). |
| `alerts`, `scan_funnel_events`, `setup_events`, `setup_event_timeline`, `hypotheses` | UI/analytics. |

Schema is created **additively** via `CREATE TABLE IF NOT EXISTS` at store init (`OpportunityRepository.__init__`, `NotificationLedger.__init__`). **There is no migration framework** — "migrations" are additive `CREATE TABLE IF NOT EXISTS` at init.

---

## 6. Opportunity layer invariants (critical)

- **Identity:** `canonical_opportunity_key()` = `TYPE:SYMBOL:DIRECTION:ANCHOR_KIND:<ns>:<anchor_level>`. `opportunity_id_from_key()` **drops the anchor_level** (legacy handle). So two keys differing only in level map to the same `opportunity_id`.
- **Repository invariants** (`OpportunityRepository._assert_identity_invariants`, enforced on every `upsert`/`apply_transition`, raising `OpportunityInvariantError` subclasses):
  1. `QuarantinedOpportunity` — the id is quarantined → write refused.
  2. `OpportunityIdentityConflict` — incoming `canonical_key` ≠ persisted column key → refused (the column key is authoritative).
  3. `TerminalStateImmutable` — a terminal row cannot be written to a non-terminal state.
- **`apply_transition(opp, from, to, reason, detail, kind, evidence_key, message)`** persists row + history + event **atomically**; dedup token is `from->to` (a genuine new transition is not suppressed by a reused evidence_key).
- **Terminal states** (`opportunity/state_machine.py`): `INVALIDATED, EXPIRED, TERMINAL, ENTRY_TRIGGERED, SUPERSEDED`.
- **Active scans** exclude terminal + quarantined: `query(active_only=True)` and `active_for_instrument()` add `NOT IN (SELECT opportunity_id FROM opportunity_quarantine)`.
- **Expiry** is anchored to the **symbol's own market time** (`view.m15_last_time`), never wall-clock; `expire_cycle(now=None)` is a no-op.

---

## 7. Notification pipeline + durable outbox

`opportunity event → hub._dispatch_opportunity_events → TelegramNotifier.notify → (durable) NotificationLedger → worker → HTTP`.

- `TelegramNotifier(cred_path, quarantine_check=..., ledger=...)`. When a `ledger` is provided the **outbox is the queue** (no in-memory queue); otherwise the legacy in-memory path is used (backward compatible).
- Outbox states: `PENDING → IN_FLIGHT → {DELIVERED, RETRY_WAIT, FAILED_PERMANENT, SUPPRESSED_QUARANTINE}`; `RETRY_WAIT → IN_FLIGHT` when due; stale `IN_FLIGHT → PENDING` (`_STALE_INFLIGHT_S=300`).
- Retries: bounded exponential backoff + jitter; HTTP 429 honours `retry_after`; `DELIVERED` set **only** on confirmed 2xx.
- **Quarantine is re-checked immediately before the HTTP send, fail-closed** (unreadable state → suppress). The hub dispatch is also fail-closed.
- **Guarantee:** durable creation + at-least-once attempts + confirmed acceptance. **No exactly-once external delivery; a TOCTOU window remains between the quarantine read and the send.** Do not claim otherwise.

---

## 8. Tests

- Framework: **pytest**. Run: `.venv\Scripts\python.exe -m pytest -q` (workdir `C:\smc_engine`).
- Local full suite (last run): **668 passed, 1 skipped, 0 failed**. The skip is data-dependent (`tests/test_causal_provenance.py:515`).
- CI (`ubuntu-latest`, Python 3.11): **659 passed, 10 skipped**. The 10 CI skips = 9 `pytest.importorskip("MetaTrader5")` tests (Windows-only package) + the same data-dependent skip.
- Notable suites: `test_notification_outbox.py` (17), `test_quarantine_notifications.py`, `test_opportunity_quarantine.py`, `test_quarantine_tool.py`, `test_identity_integrity.py`, `test_recovery.py`, `test_telegram*.py`, `test_risk*.py`, `test_execution*.py`, `test_order_guard.py`, `test_dry_run.py`.

---

## 9. Safety constraints (do not violate without explicit owner authorization)

- `LIVE_EXECUTION_ENABLED = False` (`web/hub.py:58`); `POST /api/live/{setup_id}` → **403**. Keep it false unless explicitly authorized with all live prerequisites met.
- Do not modify/disable **MT5** or **`Varis_SMC`**.
- Do not commit secrets; `telegram.txt` and the DPAPI file must stay untracked.
- Production DB changes (migrations/quarantine) are **separate authorized operations**. Prefer read-only (`file:...?mode=ro`) + SQLite backup-API copies for investigation.
- The only order-producing live path is `/api/live` (403). The demo harness is `/api/paper/{setup_id}` (+`/cancel`) and `/api/dry-run/{setup_id}`; they require a valid **setup** (entry/SL/TP) produced by the causal pipeline.

---

## 10. Current operational state

- **Deployed:** `826e466`, `stale_code=false`, DEMO AUTHORIZED, watcher running, `db_write_failures=0`.
- **Quarantine:** 1 record — an `OPP-REVERSAL-*` entry (exact id / anchor levels / timestamp redacted for this document), reason `IDENTITY_AMBIGUOUS_LIFECYCLE_UNRESOLVED`. Its **identity conflict is unresolved and deliberately not rewritten**: the indexed key holds the creation-time anchor level (authoritative) while the payload/sweep_evidence reflects later drift.
- **Notification outbox:** present; holds **1 DELIVERED** test row (Telegram message id and correlation id redacted).
- **Signal quality:** `INSUFFICIENT DATA` (no stored historical OHLC dataset, no outcome-evaluation harness).
- **Demo order acceptance:** `BLOCKED` — **no valid stop-loss**: `/api/setups` is empty and the `setups` table has 0 rows; no approved test price was supplied.
- Production DB grew to ~193 opportunities / 1661 alerts via the server's own scanning (normal).

---

## 11. Outstanding work / recommended next steps

1. **Demo order acceptance (unblock):** provide an **existing valid EURUSD BUY test setup** (or an explicitly approved test price) so a valid stop-loss can be derived. Then run `/api/paper/{setup_id}` after the full risk/policy/`order_check` chain, volume `0.01`, SL mandatory, ≤1 order, 1% per-trade / 10% daily caps. Reconcile the broker response; verify protection + cleanup.
2. **Signal quality:** build a persisted historical OHLC dataset + a reproducible outcome-evaluation harness (entry/invalidation/exit/target + spread/slippage/commission), in-sample/out-of-sample split, and sample-size/uncertainty. Only then can the status move off `INSUFFICIENT DATA`.
3. **Telegram delivery:** optionally implement a durable **delivery ledger / transactional outbox semantics across processes** if true atomic quarantine-vs-send exclusion is required. Document the remaining TOCTOU race precisely; never claim exactly-once.
4. **GBPAUD identity:** if a definitive creation-time identity can ever be established from independent causal evidence, a separately-approved migration could reconcile the key. Until then, leave it quarantined.
5. **Unrelated untracked reports** (`PHASE-9/10/12/13/14B-*.md`) are pre-existing and intentionally not committed; leave them.

---

## 12. Conventions & gotchas (learned the hard way)

- **PowerShell:** `"PID $p:"` is mis-parsed as a drive → use `"PID ${p}:..."`. For native exe args containing JSON, PowerShell strips inner quotes — use the `--%` stop-parsing operator **or** drive the code via a small Python script that calls the function with a **list** of args (most robust).
- **Inline `python -c "..."` with nested quotes fails** → write a temp `.py` script (e.g. under `%TEMP%\opencode`) and run it.
- **Git:** `git add <explicit paths>` only (never `git add -A`/`.`). Expect benign `LF will be replaced by CRLF` warnings. `git diff --check` must be clean before committing.
- **Restart is a separate operation** from migration/deployment; capture the deployed SHA, `stale_code`, process tree, and rollback target each time. Use `start_workstation.bat` to relaunch; the documented stop is unreliable (see §3).
- **DB investigation:** open `file:<path>?mode=ro` and use the SQLite **backup API** for consistent snapshots; never mutate production while investigating.
- **Read-only inspection helpers** already exist: `tools/phase16e_identity_assessment.py`, `tools/phase16g_quarantine_opportunities.py` (dry-run default).

---

## 13. Key read-only API surfaces

`GET /api/runtime` (loaded_git_commit, stale_code, live_execution_enabled, account, recovery), `/api/account`, `/api/symbols`, `/api/market/{symbol}`, `/api/risk`, `/api/setups`, `/api/opportunities[/diagnostics]`, `/api/research/funnel`, `/api/setup-history`. Mutating: `POST /api/analysis/run`, `/api/dry-run/{setup_id}`, `/api/paper/{setup_id}` (+`/cancel`), `/api/live/{setup_id}` (**403**).

---

_End of handover. When in doubt: prefer read-only evidence, keep `LIVE_EXECUTION_ENABLED=False`, do not touch MT5/`Varis_SMC`, and stop and report at any failed safety gate rather than bypassing it._
