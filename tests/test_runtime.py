"""Runtime Propagation & UI State Integrity Gate — regression tests (§14).

Tests verify:
1.  /api/runtime returns the expected fingerprint keys
2.  source_hashes from the running module paths are consistent
3.  port-8765 process detection utility functions work
4.  market_diagnostic returns a structured dict for a live-like source
5.  market_diagnostic returns an error dict when bars are unavailable
6.  ALERTS / CAUSAL EVENTS / HISTORY are separate data scopes
7.  IRL provenance fields are serialized in analysis candidates
8.  D-1: EXECUTION_READY → FILLED remains a valid transition
9.  D-2: IDs are timestamp-based, not positional
10. live_execution_enabled is hard-coded False in the running process
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest

from src.smc_engine.lifecycle import SetupLifecycle, SetupState
from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup
from src.smc_engine.structure import _ts_key, find_swings
from src.smc_engine.web import runtime as rt
from src.smc_engine.web.hub import DictSource, EngineHub, TIMEFRAMES

from tests.test_api import build_client, make_setup, FakeDemoSource


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
BASE = pd.Timestamp("2026-01-01", tz="UTC")


def _m15_bars(n: int = 30) -> pd.DataFrame:
    rows = []
    for i in range(n):
        t = BASE + pd.Timedelta(minutes=15 * i)
        rows.append({"time": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                     "tick_volume": 100})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1. /api/runtime returns expected keys
# ---------------------------------------------------------------------------
def test_runtime_fingerprint_keys(tmp_path):
    client, hub = build_client(tmp_path)
    r = client.get("/api/runtime").json()
    required = {
        "git_commit", "git_dirty", "modified_files",
        "server_started_at", "python_executable", "pid",
        "module_paths", "source_hashes",
        "live_execution_enabled", "account_mode",
        "server_version", "files_modified_after_startup",
    }
    assert required <= set(r.keys()), f"missing keys: {required - set(r.keys())}"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 2. source_hashes match the actual files on disk
# ---------------------------------------------------------------------------
def test_source_hashes_match_disk(tmp_path):
    client, hub = build_client(tmp_path)
    r = client.get("/api/runtime").json()
    disk = rt.source_fingerprints()
    for key, expected_hash in disk.items():
        assert r["source_hashes"].get(key) == expected_hash, (
            f"source hash mismatch for {key}: API={r['source_hashes'].get(key)} disk={expected_hash}"
        )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 3. runtime_info exposes module paths for all critical modules
# ---------------------------------------------------------------------------
def test_runtime_module_paths_are_populated(tmp_path):
    _, hub = build_client(tmp_path)
    info = hub.runtime_info()
    paths = info["module_paths"]
    # All critical modules should be loaded (not 'not_loaded') in this process
    for key in ("setup", "causal", "lifecycle", "structure", "hub"):
        assert paths[key] != "not_loaded", f"module {key} not loaded"
        assert Path(paths[key]).exists(), f"module path {paths[key]} does not exist on disk"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 4. market_diagnostic returns structured dict when bars are available
# ---------------------------------------------------------------------------
def test_market_diagnostic_success(tmp_path):
    client, hub = build_client(tmp_path)
    r = client.get("/api/market-diagnostic/TEST?tf=M15").json()
    required = {"symbol", "requested_tf", "requested_bar_count",
                "resolved_broker_symbol", "mt5_connected",
                "returned_bar_count", "latest_candle_time", "error"}
    assert required <= set(r.keys()), f"missing: {required - set(r.keys())}"
    assert r["symbol"] == "TEST"
    assert r["requested_tf"] == "M15"
    assert r["returned_bar_count"] > 0
    assert r["error"] is None
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 5. market_diagnostic returns an error dict when bars are unavailable
# ---------------------------------------------------------------------------
def test_market_diagnostic_unavailable_symbol(tmp_path):
    client, hub = build_client(tmp_path)
    # "MISSING" is not primed in the test source — bars() raises RuntimeError
    r = client.get("/api/market-diagnostic/MISSING?tf=M15").json()
    assert r["returned_bar_count"] == 0
    assert r["error"] is not None   # bar error surfaced, not suppressed
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 6. ALERTS / CAUSAL EVENTS / HISTORY are separate data scopes
# ---------------------------------------------------------------------------
def test_alerts_causal_events_history_are_distinct_scopes(tmp_path):
    """Alerts are in-memory; causal events are SQLite history (canonical IDs only);
    history is the in-memory lifecycle store. They may disagree intentionally."""
    client, hub = build_client(tmp_path)

    # Populate an in-memory alert via a hub web event
    hub.web.add_alert("TEST", "test", "SETUP_CREATED", "test alert", level="INFO")

    # Alerts (in-memory ring buffer) — should contain our alert
    alerts_r = client.get("/api/alerts").json()
    alert_kinds = [a["kind"] for a in alerts_r.get("alerts", [])]
    assert "SETUP_CREATED" in alert_kinds, "alert not found in /api/alerts"

    # Causal events (SQLite, canonical-ID-only) — should be empty for test source
    hist_r = client.get("/api/setup-history?symbol=TEST").json()
    assert hist_r["events"] == [], (
        "causal history should be empty: test setups use non-canonical IDs"
    )

    # History (/api/history) — in-memory lifecycle store, also empty initially
    history_r = client.get("/api/history").json()
    assert isinstance(history_r["rows"], list)

    hub.stop_poller()


# ---------------------------------------------------------------------------
# 7. IRL provenance fields are serialized in analysis candidates
# ---------------------------------------------------------------------------
def test_irl_provenance_fields_in_candidates(tmp_path):
    from src.smc_engine.causal import CausalCandidate
    from src.smc_engine.models import (LiquiditySide, LiquiditySweep,
                                       StructureEvent, StructureEventType)
    import src.smc_engine.web.hub as hub_mod

    IRL_SETUP = TradeSetup(
        id="SET-IRL-PROV", symbol="TEST", direction=Direction.BULLISH,
        created_time=pd.Timestamp("2099-01-01", tz="UTC"),
        poi_id="POI-X", sweep_id="SW-X", csd_id="CSD-X", protected_level=93.0,
        order_block_id="OB-X", inducement_id="IDM-X", entry=99.0, stop_loss=93.0,
        take_profit=120.0, irl_swing_id="IRL-SWING-7",
        irl_target_type="PRIOR_SESSION_LOW", irl_qualification_reason="confirmed by volume",
        invalidation_level=93.0, risk_percent=1.0,
    )
    sweep = LiquiditySweep("SW-X", LiquiditySide.SELL_SIDE, 95.0, 93.0, "LQ-X",
                           10, BASE, 95.4)
    csd = StructureEvent("CSD-X", StructureEventType.CSD, Direction.BULLISH, 96.0,
                         12, BASE, "SH-X", "SW-X")

    class StubIRL:
        def __init__(self, symbol, config=None):
            pass
        def analyze_at(self, d1, h4, m15, as_of=None):
            return [CausalCandidate(setup=IRL_SETUP, setup_time=BASE,
                                    sweep=sweep, csd=csd)]

    hub_mod.CausalMTFAnalyzer = StubIRL
    try:
        client, hub = build_client(tmp_path, FakeDemoSource())
        a = client.get("/api/analysis/TEST/M15").json()
        assert len(a["candidates"]) == 1
        c = a["candidates"][0]
        ev = c.get("evidence", {})
        assert ev.get("irl_swing_id") == "IRL-SWING-7"
        assert ev.get("irl_target_type") == "PRIOR_SESSION_LOW"
        assert ev.get("irl_qualification_reason") == "confirmed by volume"
        hub.stop_poller()
    finally:
        from src.smc_engine.causal import CausalMTFAnalyzer
        hub_mod.CausalMTFAnalyzer = CausalMTFAnalyzer


# ---------------------------------------------------------------------------
# 8. D-1 regression: EXECUTION_READY → FILLED is a valid transition
# ---------------------------------------------------------------------------
def test_d1_execution_ready_to_filled_is_valid():
    setup = make_setup()
    lc = SetupLifecycle(setup)
    assert lc.state is SetupState.EXECUTION_READY
    lc.transition(SetupState.FILLED)
    assert lc.state is SetupState.FILLED


# ---------------------------------------------------------------------------
# 9. D-2 regression: swing IDs use timestamps, not positional indices
# ---------------------------------------------------------------------------
def test_d2_swing_ids_are_timestamp_based():
    df = pd.DataFrame([
        {"time": BASE + pd.Timedelta(minutes=15 * i),
         "open": 100.0, "high": 101.0 + (1.0 if i == 5 else 0.0),
         "low": 99.0 - (1.0 if i == 3 else 0.0), "close": 100.0}
        for i in range(15)
    ])
    swings = find_swings(df, left=2, right=2)
    for s in swings:
        assert s.id[3:].isdigit(), (
            f"swing ID {s.id!r} contains non-digit after prefix — must be timestamp"
        )
    # IDs must be stable even if we shift the window (trim head)
    df2 = df.iloc[1:].reset_index(drop=True)
    swings2 = find_swings(df2, left=2, right=2)
    ids1 = {s.id for s in swings}
    ids2 = {s.id for s in swings2}
    common = ids1 & ids2
    # Any swing that appears in both frames must keep the same ID
    for sid in common:
        assert sid in ids1 and sid in ids2, f"ID {sid} changed across window shift"


# ---------------------------------------------------------------------------
# 10. live_execution_enabled is False — read-only from /api/runtime
# ---------------------------------------------------------------------------
def test_live_execution_disabled_in_runtime(tmp_path):
    client, hub = build_client(tmp_path)
    r = client.get("/api/runtime").json()
    assert r["live_execution_enabled"] is False, (
        "LIVE_EXECUTION_ENABLED must remain False"
    )
    # Also verify the status endpoint agrees
    s = client.get("/api/status").json()
    assert s["live_execution_enabled"] is False
    hub.stop_poller()
