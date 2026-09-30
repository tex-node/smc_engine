# Session Summary

## Project
`tex-node/smc_engine` — Smart Money Concepts (Varis methodology) 3-layer engine on MT5, plus a browser workstation GUI. GitHub is the source of truth; local `C:\smc_engine` mirrors it.

## Where we are
- **Branch `feature/gui-workstation`**, app lineage: `3991d38` (real-data GUI) → readiness slice (this commit, `feat(gui): add causal setup readiness monitoring`). CI green on lineage. Tree clean after this commit.
- **GATE A: PASS** — demo BUY_LIMIT ticket 3301052487 round-tripped; evidence log `%TEMP%\opencode\gate_a_result.log`.
- **GATE B: BLOCKED — NO CAUSAL TRADESETUP** — all 22 Exness symbols return `candidates=0` (two samples); synthetic capability probe yields 1 genuine causal candidate (path alive); real feed reaches CSD (EURUSD×2, EURAUD×1, AUDUSD×1) with no completed M15 continuation. No order placed, no engine change. Resume when `/api/setups` shows a `SETUP-*` id.
- Test suite: **150 passed** (engine + fvg + api + gui-smoke + orchestration + real-integration + 7 readiness). Linux-sim: 27p/9s.
- GUI workstation: running at `http://127.0.0.1:8765` attached to **Exness-MT5Trial9 demo** (login 477217728), `paper_enabled=True`, `live_execution_enabled=False`.

## Session arc
1. **Engine lineage** (main): legacy `varis_smc_bot.py` v1 → v2 dynamic 1% risk sizing → dedup guard → pending-order invalidation/cancel.
2. **Deployment**: mirrored `d569692`, `.venv` isolated, 102/102 tests, 8-point offline smoke PASS.
3. **DryRunEngine demo validation**: real metadata/bid/ask/`order_check retcode=0`, zero sends; DPAPI-based account gate (demo/trial only) caught and refused an accidental Headway-Real attach.
4. **GUI workstation** (FastAPI + SSE + static dark canvas frontend over the engine; engine authoritative; FVG added as engine primitive) → hub orchestration slice (canonical enum, idempotent ticket accounting, symbol isolation, dead-code) → real-data GUI integration (evidence passthrough, quote strip, MARKET DATA UNAVAILABLE, 9 new tests). 143 green.
5. **GATE A: PASS** (this session, demo round-trip):
   - Run 1 exposed a harness attach-hijack (`market.initialize()` re-attached module to Headway-Real); per-send account gate denied every attempt — zero broker sends, non-mutating `order_check` only. Harness fixed (harness-only change) to reuse the explicit demo connection.
   - Run 2 full evidence: 6/6 DENIED-OK probes; policy BUY_LIMIT vol=0.05 from real Exness metadata; `order_check retcode=0`; `order_send retcode=10009` exactly once; ticket 3301052487 in `pending(magic)`; registry active; comment identity; controlled invalidation → PROTECTED_LEVEL_BREACHED (terminal; own-ticket REMOVE); idempotent replay → 0 extra sends; positions 0; pending 0; foreign tickets 0; audit `[PENDING, REMOVE-own]`. Credentials: DPAPI single-use blob deleted by launcher, no plaintext in logs.
6. **GATE B: BLOCKED — NO CAUSAL TRADESETUP** (this session):
   - Production scan: all 22 Exness demo symbols × causal path → `candidates=0` (two independent samples).
   - Capability probe (synthetic complete-timeline specimen, engine unmodified): `CausalMTFAnalyzer.analyze_at` produced **1 genuine candidate** with full evidence chain (D1 POI → H4 sweep → CSD → M15 OB → IDM → IRL) — proving the path is alive, not dead.
   - Real-feed stage trace: EURUSD reaches CSD ×2, EURAUD ×1, AUDUSD ×1 — but no post-CSD M15 continuation completes currently. Honest market condition, not a defect.
   - Per §4/§36: no synthetic fallback, no injected setup, no order placed, no engine/GUI code changed. Gate B execution remains pending the first genuine causal setup (live poller on the GUI server watches registered symbols; GUI will show `NO CAUSAL SETUP DETECTED` until one appears).
7. **GATE B READINESS LAYER** (observation-only): `/api/readiness` derived from the engine lifecycle (genuine `SETUP-*` id + complete fields only); GATE B strip in the workstation (WAITING → READY FOR MANUAL VALIDATION → REVIEW SETUP selects for inspection, never executes); dedup by backend setup id via localStorage (refresh-safe); readiness polling is read-only with zero broker calls; incomplete/foreign-id objects rejected at the hub boundary (`register_setup` completeness guard) and by readiness itself. Tests: 7 new (`tests/test_readiness.py`), full suite 150 green. Verified live on Exness demo: all symbols `WAITING_FOR_CAUSAL_SETUP`, `live_execution_enabled=false`, `/api/live` 403.

## Standing constraints
- **LIVE EXECUTION DISABLED** — do not enable; `/api/live/*` 403; no live route; account + demo-server + paper gates remain mandatory.
- Hub stays an orchestrator; no SMC/risk math in the frontend; setup IDs are the only handle the GUI passes to execution.
- Gate B resume path: when a real candidate appears (`SETUP-` id in `/api/setups`), drive it through the GUI paper flow; the first Gate B order may then proceed with the same allow-list + one-send + cancel discipline proven in Gate A.
- Credential hygiene: DPAPI CurrentUser single-use blobs; provisioner self-deletes; password never in args/logs/commits.

## Run
```powershell
cd C:\smc_engine
.venv\Scripts\python.exe -m uvicorn smc_engine.web.api:create_app --factory --host 127.0.0.1 --port 8765
# http://127.0.0.1:8765  (env MT5_LOGIN/PASSWORD/SERVER for explicit demo attach)
python -m pytest -q      # 150 tests (incl. 7 readiness GUI-pipeline tests)
```
Gate A evidence: `%TEMP%\opencode\gate_a_result.log`. Gate B traces/probes: `%TEMP%\opencode\gate_b_*.py|log` (not application code).
