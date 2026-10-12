# Session Summary

## Project
`tex-node/smc_engine` (PUBLIC) — Smart Money Concepts (Varis methodology) engine on MT5, plus a browser workstation GUI. Three layers: (1) causal/strategy (observation-only), (2) opportunity state machine, (3) workstation/API + durable Telegram notifications. GitHub is the source of truth; local `C:\smc_engine` mirrors it.

## Where we are
- **Branch `feature/gui-workstation`** (remote `https://github.com/tex-node/smc_engine.git`, account `tex-node`).
- **HEAD `fa20eb6`** (docs: handover note, secrets redacted) · **deployed production revision `826e466`**.
  Lineage: `09c7ce2` (per-symbol market-time expiry) → `4682f63` (Phase 16E identity/terminal invariants) → `af5c4fe` (quarantine safeguards) → `826e466` (durable Telegram outbox) → `78e5169` (16H.7–16H.9 evidence) → `fa20eb6` (handover).
- **Tests:** local full suite **668 passed, 1 skipped** (`tests/test_causal_provenance.py:515`, data-dependent). CI (ubuntu-latest, Py 3.11) **659 passed, 10 skipped** (9 `importorskip("MetaTrader5")` + 1 data-dependent). CI green on the lineage.
- **Deployment:** running via `start_workstation.bat`; `/api/runtime` → `loaded_git_commit=826e466`, `stale_code=false`, DEMO `AUTHORIZED`, watcher healthy, `db_write_failures=0`.
- **`LIVE_EXECUTION_ENABLED=False`**; `POST /api/live/{setup_id}` → **403**.

## Opportunity layer (current core)
- Modules: `src/smc_engine/opportunity/{engine,repository,models,evaluator,state_machine,arbiter}.py`.
- States + **terminal set**: `INVALIDATED, EXPIRED, TERMINAL, ENTRY_TRIGGERED, SUPERSEDED`.
- **Identity:** `canonical_opportunity_key()` includes `anchor_level`; `opportunity_id_from_key()` drops it (legacy handle) → two keys can share one `opportunity_id`.
- **Repository invariants** (`_assert_identity_invariants`, on every write): `QuarantinedOpportunity` → `OpportunityIdentityConflict` (indexed `canonical_key` authoritative) → `TerminalStateImmutable`. `apply_transition()` commits row + history + event atomically (dedup token `from->to`).
- **Expiry** anchored to the symbol's own market time (`view.m15_last_time`), never wall-clock; `expire_cycle(now=None)` is a no-op.
- **Arbitration** supersedes competing same-symbol/same-direction theses.

## Telegram notification pipeline (durable outbox)
- `telegram.py` (`TelegramNotifier`) + `notification_ledger.py` (`NotificationLedger`, table `notification_outbox`). When a ledger is provided, **the outbox is the queue**.
- States: `PENDING → IN_FLIGHT → {DELIVERED, RETRY_WAIT, FAILED_PERMANENT, SUPPRESSED_QUARANTINE}`; stale `IN_FLIGHT → PENDING`; bounded backoff+jitter; 429 `retry_after`; `DELIVERED` only on confirmed 2xx.
- **Quarantine re-checked immediately before the HTTP send, fail-closed.** **No exactly-once; TOCTOU window remains.** Durable creation + at-least-once attempts + confirmed acceptance only.

## Operational state
- **Quarantine:** 1 record — an `OPP-REVERSAL-*` entry, reason `IDENTITY_AMBIGUOUS_LIFECYCLE_UNRESOLVED`. Its **identity conflict is unresolved and deliberately not rewritten** (indexed key holds the creation-time anchor level; payload/sweep_evidence reflects later drift). Preserve it.
- **Notification outbox:** `notification_outbox` table present (production migration applied); holds 1 DELIVERED test row.
- **Telegram:** one authorized real test delivered (16H.9), via the app's outbox + sender path.
- **Demo order acceptance:** **BLOCKED — no valid stop-loss** (`/api/setups` empty, `setups` table 0 rows, no approved test price). No order placed.
- **Signal quality:** **`INSUFFICIENT DATA`** (no stored historical OHLC dataset / outcome-evaluation harness).

## Session arc
1. **Engine lineage**: legacy `varis_smc_bot.py` v1 → v2 dynamic 1% risk sizing → dedup guard → pending-order invalidation/cancel.
2. **Deployment**: mirrored `d569692`, `.venv` isolated, 102/102 tests, 8-point offline smoke PASS.
3. **DryRunEngine demo validation**: real metadata/bid/ask/`order_check retcode=0`, zero sends; DPAPI account gate refused an accidental Headway-Real attach.
4. **GUI workstation** (FastAPI + SSE + static frontend; FVG primitive; hub orchestration; real-data integration). 143 green.
5. **GATE A: PASS** — demo BUY_LIMIT ticket `3301052487` round-tripped (allow-list + one-send + cancel discipline); evidence `%TEMP%\opencode\gate_a_result.log`.
6. **GATE B: BLOCKED — NO CAUSAL TRADESETUP** — all 22 Exness symbols `candidates=0`; synthetic probe proved the causal path alive; no order placed.
7. **GATE B READINESS LAYER** (observation-only) + **CAUSAL SETUP EVENT HISTORY** (`web/history.py`: `setup_events` + `setup_event_timeline`, immutable first snapshot, EXPIRED/CLOSED/REVIVED transitions). Suite 166 green.
8. **Opportunity layer** (Phases 8–16): explicit state machine, arbitration, deduplicated lifecycle events, DB writer lock (`web/dbwrite.py`), market-event–anchored identity/TTL.
9. **Recovery hardening** (`09c7ce2`): removed the global wall-clock TTL sweep; per-symbol market-time expiry; runtime diagnostics (`loaded_git_commit`, `stale_code`, recovery counts).
10. **Phase 16E** (`4682f63`): canonical identity collision + terminal-state immutability + atomic transitions; `tests/test_identity_integrity.py`.
11. **Phase 16H / 16H.1** (`af5c4fe`): auditable, idempotent **quarantine** (`opportunity_quarantine`), active-scan exclusion, write refusal, per-opportunity conflict isolation, send-boundary quarantine check, fail-closed storage; tool `tools/phase16g_quarantine_opportunities.py` (dry-run-first).
12. **Phase 16H.5–16H.7**: production **quarantine applied** (GBPAUD, not rewritten); **durable Telegram outbox** committed/pushed (`826e466`, CI green); additive `notification_outbox` **migration applied** to production; deployment accepted.
13. **Phase 16H.9**: redeployed `826e466`; **one Telegram test delivered**; **demo order BLOCKED** (no valid stop-loss); signal quality `INSUFFICIENT DATA`.
14. **Handover** (`fa20eb6`): `HANDOVER.md` added, sensitive env details redacted, tip amended + force-pushed (old blob off the branch tip; dangling object per accepted caveat).

## Standing constraints
- **LIVE EXECUTION DISABLED** — do not enable; `/api/live/*` → 403; account + demo-server + paper gates mandatory.
- **Do not modify MT5 or the `Varis_SMC` EA.**
- **Credential hygiene:** `telegram.txt` and the DPAPI blob stay **untracked**; DPAPI CurrentUser single-use; never print tokens/chat ids/account secrets.
- **Production DB changes** (migrations/quarantine) are separate authorized operations; investigate read-only (`file:...?mode=ro`) + SQLite backup-API copies.
- **Quarantine must be preserved**; do not rewrite the GBPAUD identity/events/history.
- **Honest guarantees:** no exactly-once Telegram delivery; no strategy-profitability claim without reproducible outcome evidence.

## Run
```powershell
cd C:\smc_engine
.venv\Scripts\python.exe -m uvicorn smc_engine.web.api:create_app --factory --host 127.0.0.1 --port 8765
# http://127.0.0.1:8765   (or use start_workstation.bat; stop_workstation.bat is unreliable — see HANDOVER.md)
.venv\Scripts\python.exe -m pytest -q      # 668 passed, 1 skipped (local); CI: 659 passed, 10 skipped
```
Read-only helpers: `tools/phase16e_identity_assessment.py`, `tools/phase16g_quarantine_opportunities.py` (dry-run default). Gate A/B evidence under `%TEMP%\opencode\`.
See **`HANDOVER.md`** for the full architecture map, invariants, gotchas, and next steps.
