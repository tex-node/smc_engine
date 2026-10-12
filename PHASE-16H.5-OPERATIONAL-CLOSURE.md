# PHASE-16H.5 — OPERATIONAL CLOSURE EVIDENCE REPORT

**Repository:** `tex-node/smc_engine` · **Branch:** `feature/gui-workstation`
**Starting commit:** `af5c4fe109bd5d4170eb45ec379791e12b5c49f9` (parent `4682f637f944dadc6362edf5ab363cbe63906003`)
**Final release commit:** `af5c4fe` (unchanged — no corrective commit was required)

> Observed facts are stated as measured; assumptions are labelled. No credentials, tokens, or account secrets are included.

---

## 1. Branch, remote, and commit
* Remote `origin = https://github.com/tex-node/smc_engine.git`; pre-push origin tip `4682f63`, post-push `af5c4fe` (fast-forward, no divergence, **no force-push**).
* HEAD `af5c4fe`; the ten-file Phase 16H.3 commit is intact; working tree clean except the preserved untracked `PHASE-9/10/12/13/14B` reports.

## 2. GitHub Actions (exact-SHA)
* Workflow `test` (`.github/workflows/test.yml`): triggers `push`/`pull_request`; `ubuntu-latest`; Python 3.11; installs `pandas numpy pytest fastapi httpx uvicorn` + `pip install --no-deps -e .`; `PYTHONPATH=.`; `pytest -q`; no secrets/services.
* **Run:** `38201372124` · https://github.com/tex-node/smc_engine/actions/runs/38201372124 · commit `af5c4fe` · job `pytest` **✓ success** (1m44s) · **conclusion: success**.
* **CI totals:** 642 passed, 10 skipped, 0 failed (total 652). The 10 skips = 9 `pytest.importorskip("MetaTrader5")` tests (Windows-only package, unavailable on Linux) + 1 data-dependent (`test_causal_provenance.py:515`). Local run: 651 passed, 1 skipped (same 652 total). **No failures.**

## 3. Local regression (exact SHA, isolated worktree)
| command | env | result | exit |
|---|---|---|---|
| `pytest <9 focused files> -q` | worktree @ `af5c4fe` | 131 passed | 0 |
| `pytest -q` | worktree @ `af5c4fe` | 651 passed, 1 skipped | 0 |

## 4. Production preflight and backup
* Canonical DB: `C:\smc_engine\smc_engine_gui.sqlite3` (`SMC_GUI_DB_DIR` unset → `./` under the launcher's `cd /d C:\smc_engine`); `db_path_basename=smc_engine_gui.sqlite3` confirmed via `/api/runtime`.
* Fresh backup (SQLite backup API, outside the repo): `…\smc16h2\backups\smc_engine_gui_pre16h5_20261012T040357Z.sqlite3`; **SHA-256 `d81c27e16edd4cbefc75dff1bdb38090686f34dd73a89205647bbfd72f76fe3b`** (identical to the 16H.2 backups → DB unchanged); `integrity_check=ok`; `foreign_key_check=[]`; schema `user_version=0`, `schema_version=1`.
* Counts before: `opportunities=187`, `opportunity_events=1549`, `opportunity_state_history=3555`, `alerts=1647`.
* Restore-verified: the backup opens read-only and yields the same counts/integrity (used for every read-only check).

## 5. GBPAUD identity diagnosis (read-only, no rewrite)
* Row `OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH`: indexed `canonical_key` = `…:1.89623`; payload `canonical_key` + `sweep_evidence.swept_level` = `…:1.89628`; state `EXPIRED`.
* Cause: the pre-fix `upsert` `ON CONFLICT(opportunity_id) DO UPDATE` updated `payload_json` but **never** `canonical_key`, so the indexed key is **frozen at creation-time** (first INSERT) while the payload carries the later drift. The `OPPORTUNITY_CREATED` event evidence key carries only the ns (`SWEEP:1791518400000000000`), not the level.
* **Creation-time identity = the indexed key (`…1.89623`)** (frozen at creation). The payload value is later drift. **No historical record was rewritten**; the conflict is retained and quarantined.
* Future writes cannot change an existing identity: `_assert_identity_invariants` raises `OpportunityIdentityConflict` (af5c4fe; tests `test_gbpaud_collision_scenario_fixture`, `test_identity_collision_is_refused_and_original_untouched`).

## 6. Quarantine migration + application (production)
* Additive table `opportunity_quarantine(opportunity_id PK, reason, at, evidence_json)` created by the repository (no destructive change); applied via `tools/phase16g_quarantine_opportunities.py` (dry-run first, then `--apply` with `--expect-db`, `--backup`, `--expect-count 187`).
* **Record:** `OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH` · reason `IDENTITY_AMBIGUOUS_LIFECYCLE_UNRESOLVED` · at `2026-10-12T04:05:50Z` · evidence `{indexed_key …1.89623, payload_key …1.89628, instrument GBPAUD, lifecycle_state EXPIRED}`.
* Result: `newly_quarantined=true`, `row_unchanged=true`, events 32 / history 67 preserved.
* Post-verification (read-only): `integrity_check=ok`, `foreign_key_check=[]`; quarantine rows = 1; GBPAUD excluded from active; counts unchanged (`{ENTRY_TRIGGERED:52, EXPIRED:115, INVALIDATED:12, SUPERSEDED:8}`; totals opps 187 / alerts 1647 / events 1549 / history 3555); **idempotent** re-run → `already_quarantined=true`, no change.
* Write-rejection proven on a disposable copy: `upsert` and `apply_transition` raise `QuarantinedOpportunity`; other 9 GBPAUD rows remain eligible.
* **Only change:** +1 quarantine table +1 record. No opportunity/alert/history/event row changed.

## 7. Deployment and service verification
* Deployed via the documented launcher (`start_workstation.bat` → `load_mt5_credentials.ps1`, DPAPI creds). Launcher: credentials CONFIGURED, account `…DEMO`, identity gate PASSED, **Live Execution DISABLED**.
* `/api/runtime`: `loaded_git_commit=af5c4fe`, `git_commit=af5c4fe`, **`stale_code=false`**, `files_modified_after_startup=[]`, `live_execution_enabled=false`, `account_identity=AUTHORIZED`, `pid=21812`.
* Process tree: `31868 (cmd launcher) → 22512 (.venv python) → 21812 (listener 127.0.0.1:8765)`.
* Diagnostics: **`quarantined_count=1`** (read by the live server); watcher running; 24-symbol universe scanning; `db_write_failures=0`; `telegram_enabled=true`, `telegram_send_count=0`.
* **Rollback target:** stop the current tree; previous verified revision `4682f63`/`09c7ce2`; restore the §4 backup if needed.

## 8. Telegram reliability — strongest supported guarantee
* Send-boundary quarantine check (`_send_with_retry` → `_is_quarantined`) is **point-in-time** and **fail-closed** (unknown → suppress); it suppresses messages queued **before** quarantine (race test). The hub dispatch filter is an additional fail-closed per-cycle filter.
* **Remaining limitations (not fixed this phase):** a **TOCTOU window** between the state read and the HTTP send; **no durable delivery ledger**; ambiguous HTTP outcomes are at-most-once (no idempotency key). **Exactly-once delivery is not claimed.** A durable outbox/ledger remains a separately-reviewed follow-up — it was **not** implemented here.

## 9. MT5 and execution readiness
* MT5: 4× `terminal64.exe` running, untouched; account `…@Exness-MT5Trial9 DEMO`, `account_identity=AUTHORIZED`.
* `LIVE_EXECUTION_ENABLED=False` (hard compile-time gate); `/api/live/place` → **403**.
* Dry-run/execution harness present (`dry_run.py`, `execution.py`, `execution_policy.py`, `order_guard.py`, `risk.py`).
* **No order was placed (demo or live).** Live prerequisites are not demonstrably satisfied: no verified signal-quality evidence, no documented per-trade/aggregate risk limits for this phase, no completed demo acceptance run. → **`LIVE EXECUTION BLOCKED`.**

## 10. Signal quality
* The scan/funnel pipeline is active (≈45k `scan_funnel_events`; 187 opportunities) but there is **no reproducible strategy-outcome evidence** (no stored historical OHLC dataset; no entry/exit/stop/target outcome tracking; no cost/slippage accounting).
* **Status remains `INSUFFICIENT DATA`.** No minimum sample size or profitability threshold was invented.

## 11. Unresolved issues / owners
| issue | status | owner |
|---|---|---|
| GBPAUD identity mismatch (`1.89623` vs `1.89628`) | quarantined; not rewritten | next authorized migration, if a definitive identity is ever established |
| Telegram TOCTOU + no durable delivery ledger | open | separate approved change |
| Live-trading prerequisites | unmet | product/strategy owner |
| Signal quality | `INSUFFICIENT DATA` | data/strategy owner |

## 12. Gate table
| Gate | Result |
|---|---|
| Commit integrity | **PASS** (`af5c4fe`, parent `4682f637…`) |
| Scope control | **PASS** (ten files; unrelated reports excluded) |
| GitHub Actions (exact SHA) | **PASS** (run 38201372124, success) |
| Local full regression | **PASS** (651/1) |
| Focused regression | **PASS** (131) |
| Backup verified | **PASS** (SHA `d81c27e1…`, integrity ok) |
| Migration/schema | **PASS** (additive; integrity ok) |
| Quarantine applied + verified | **PASS** (1 record; row preserved; idempotent) |
| Deployment + health | **PASS** (`af5c4fe`, not stale, quarantine read, 0 db failures) |
| Documentation accuracy | **PASS** (no unsupported delivery/strategy claims) |
| Notification reliability | **CONDITIONAL** (point-in-time; TOCTOU; no durable ledger) |
| MT5 identity | **PASS** (DEMO, AUTHORIZED) |
| Demo execution acceptance | **BLOCKED** (not run; prerequisites unmet) |
| Live execution | **BLOCKED** (gate False; prerequisites unmet) |
| Signal quality | **INSUFFICIENT DATA** |

**Overall: `PRODUCTION QUARANTINE COMPLETE — LIVE TRADING BLOCKED`.**
