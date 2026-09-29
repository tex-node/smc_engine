"""GUI -> real engine integration slice.

Proves the workstation renders engine truth: causal candidates with their
original evidence, exact value passthrough, symbol/timeframe integrity,
backend-driven risk, lifecycle canonicalization, and the hard rule that no
UI-facing layer can reach order_send() except through hub gates.
"""
import ast
import re
from pathlib import Path

import pandas as pd
import pytest

import src.smc_engine.web.hub as hub_mod
from src.smc_engine.causal import CausalCandidate
from src.smc_engine.models import (Direction, LiquiditySide, LiquiditySweep,
                                   StructureEvent, StructureEventType)
from src.smc_engine.setup import TradeSetup

from tests.test_api import FakeDemoSource, build_client, make_setup

BASE = pd.Timestamp("2026-01-01", tz="UTC")

REAL_SETUP = TradeSetup(
    id="SET-REAL", symbol="TEST", direction=Direction.BULLISH, created_time=BASE,
    poi_id="POI-9001", sweep_id="SW-77", csd_id="CSD-77", protected_level=93.77,
    order_block_id="OB-450", inducement_id="IDM-451", entry=99.37, stop_loss=93.77,
    take_profit=106.51, irl_swing_id="S-IRL-9", invalidation_level=93.77, risk_percent=1.0)
REAL_SWEEP = LiquiditySweep("SW-77", LiquiditySide.SELL_SIDE, 95.0, 93.77, "LQ-5",
                            10, BASE + pd.Timedelta(hours=40), 95.4)
REAL_CSD = StructureEvent("CSD-77", StructureEventType.CSD, Direction.BULLISH, 96.2,
                          12, BASE + pd.Timedelta(hours=48), "SH-2", "SW-77")


class StubAnalyzer:
    candidates = []

    def __init__(self, symbol, config=None):
        self.symbol = symbol

    def analyze_at(self, d1, h4, m15, as_of=None):
        return list(StubAnalyzer.candidates)


@pytest.fixture()
def stubbed(monkeypatch):
    monkeypatch.setattr(hub_mod, "CausalMTFAnalyzer", StubAnalyzer)
    StubAnalyzer.candidates = []
    yield
    StubAnalyzer.candidates = []


def test_real_setup_values_pass_through_exactly(tmp_path, stubbed):
    StubAnalyzer.candidates = [CausalCandidate(setup=REAL_SETUP, setup_time=BASE,
                                               sweep=REAL_SWEEP, csd=REAL_CSD)]
    client, hub = build_client(tmp_path, FakeDemoSource())
    a = client.get("/api/analysis/TEST/M15").json()
    assert len(a["candidates"]) == 1
    c = a["candidates"][0]
    # exact numbers, no rounding/reconstruction anywhere
    for field, val in [("entry", 99.37), ("stop_loss", 93.77), ("take_profit", 106.51),
                       ("protected_level", 93.77), ("invalidation_level", 93.77),
                       ("risk_percent", 1.0)]:
        assert c[field] == val, field
    assert c["direction"] == "BULLISH"
    ev = c["evidence"]
    assert ev["sweep"]["swept_level"] == 95.0
    assert ev["sweep"]["sweep_extreme"] == 93.77
    assert ev["sweep"]["side"] == "SELL_SIDE"
    assert ev["csd"]["level"] == 96.2 and ev["csd"]["id"] == "CSD-77"
    assert ev["poi_id"] == "POI-9001" and ev["order_block_id"] == "OB-450"
    assert ev["inducement_id"] == "IDM-451" and ev["irl_swing_id"] == "S-IRL-9"
    hub.stop_poller()


def test_no_causal_setup_is_a_valid_result(tmp_path, stubbed):
    client, hub = build_client(tmp_path, FakeDemoSource())
    a = client.get("/api/analysis/TEST/M15").json()
    assert a["candidates"] == []          # empty, not fabricated, not an error
    assert a["timeframe"] == "M15"
    assert "last_closed_candle_time" in a
    assert client.get("/api/setups?symbol=TEST").json()["setups"] == []
    hub.stop_poller()


def test_symbol_isolation_with_real_candidates(tmp_path, stubbed):
    from dataclasses import replace
    aud = replace(REAL_SETUP, id="SET-AUD", symbol="EURAUD")
    xau = replace(REAL_SETUP, id="SET-XAU", symbol="XAUUSD", direction=Direction.BEARISH)

    class PerSymbolStub(StubAnalyzer):
        def analyze_at(self, d1, h4, m15, as_of=None):
            src = {"EURAUD": [CausalCandidate(setup=aud, setup_time=BASE,
                                              sweep=REAL_SWEEP, csd=REAL_CSD)],
                   "XAUUSD": [CausalCandidate(setup=xau, setup_time=BASE,
                                              sweep=REAL_SWEEP, csd=REAL_CSD)],
                   "TEST": []}
            return list(src.get(self.symbol, []))

    hub_mod.CausalMTFAnalyzer = PerSymbolStub
    try:
        client, hub = build_client(tmp_path, FakeDemoSource())
        src = hub.source
        d = pd.DataFrame([[BASE + pd.Timedelta(days=i), 100, 101, 99, 100] for i in range(200)],
                         columns=["time", "open", "high", "low", "close"])
        from src.smc_engine.web.hub import TIMEFRAMES
        for sym in ("EURAUD", "XAUUSD"):
            for tf in ("D1", "H4", "M15"):
                src.prime(sym, TIMEFRAMES[tf], d)
        assert client.get("/api/analysis/EURAUD/M15").status_code == 200
        assert client.get("/api/analysis/XAUUSD/M15").status_code == 200
        aud_rows = client.get("/api/setups?symbol=EURAUD").json()["setups"]
        xau_rows = client.get("/api/setups?symbol=XAUUSD").json()["setups"]
        assert {r["setup_id"] for r in aud_rows} == {"SET-AUD"}
        assert {r["setup_id"] for r in xau_rows} == {"SET-XAU"}
        assert all(r["symbol"] == "EURAUD" for r in aud_rows)
        # normalized query still isolates
        assert client.get("/api/setups?symbol=xauusd.").json()["setups"][0]["setup_id"] == "SET-XAU"
        hub.stop_poller()
    finally:
        hub_mod.CausalMTFAnalyzer = StubAnalyzer


def test_timeframe_not_silently_substituted(tmp_path, stubbed):
    from src.smc_engine.web.hub import TIMEFRAMES
    src = FakeDemoSource()
    # prime ONLY M30 + the causal trio
    d = pd.DataFrame([[BASE + pd.Timedelta(minutes=30 * i), 100, 101, 99, 100] for i in range(120)],
                     columns=["time", "open", "high", "low", "close"])
    src.prime("TEST", TIMEFRAMES["M30"], d)
    for tf in ("D1", "H4", "M15"):
        src.prime("TEST", TIMEFRAMES[tf], d)
    client, hub = build_client(tmp_path, src)
    hub.source._bars.pop(("TEST", TIMEFRAMES["M15"]), None)  # make M15 unavailable
    hub.bars_cache.clear()
    r = client.get("/api/analysis/TEST/M15")
    assert r.status_code == 503        # no H1/M30 data sneaking into an M15 request
    r30 = client.get("/api/analysis/TEST/M30")
    assert r30.status_code == 200 and r30.json()["timeframe"] == "M30"
    hub.stop_poller()


def test_quote_is_source_data_not_fabricated(tmp_path, stubbed):
    client, hub = build_client(tmp_path, FakeDemoSource())
    q = client.get("/api/analysis/TEST/M15").json()["quote"]
    assert q["market_data"] in ("AVAILABLE", "UNAVAILABLE")
    if q["market_data"] == "AVAILABLE":
        assert q["spread"] == pytest.approx(q["ask"] - q["bid"])
        assert q["source"] == "fakedemo"
    hub.stop_poller()


def test_risk_display_tracks_backend_result(tmp_path, stubbed):
    """Changing the backend risk inputs must change what the API reports."""
    StubAnalyzer.candidates = [CausalCandidate(setup=REAL_SETUP, setup_time=BASE,
                                               sweep=REAL_SWEEP, csd=REAL_CSD)]
    src1 = FakeDemoSource()
    client, hub = build_client(tmp_path, src1)
    client.get("/api/analysis/TEST/M15")
    r1 = client.get("/api/risk?symbol=TEST&setup_id=SET-REAL").json()
    direct1 = hub.risk_engine("TEST").volume_for_risk(
        10000.0, REAL_SETUP.risk_percent, REAL_SETUP.entry, REAL_SETUP.stop_loss).volume
    src1.account_info = dict(src1.account_info, balance=20000.0, equity=20000.0)
    r2 = client.get("/api/risk?symbol=TEST&setup_id=SET-REAL").json()
    assert r1["new_setup"]["volume"] == direct1       # GUI value == RiskEngine value
    assert r2["new_setup"]["volume"] > r1["new_setup"]["volume"]
    assert r1["equity"] == 10000.0 and r2["equity"] == 20000.0
    hub.stop_poller()


def test_lifecycle_state_is_canonical(tmp_path, stubbed):
    StubAnalyzer.candidates = [CausalCandidate(setup=REAL_SETUP, setup_time=BASE,
                                               sweep=REAL_SWEEP, csd=REAL_CSD)]
    client, hub = build_client(tmp_path, FakeDemoSource())
    client.get("/api/analysis/TEST/M15")
    row = client.get("/api/setups?symbol=TEST").json()["setups"][0]
    assert row["state"] == "EXECUTION_READY" and row["display"] == "EXECUTION_READY"
    from src.smc_engine.lifecycle import SetupState
    hub._transition("SET-REAL", SetupState.PROTECTED_LEVEL_BREACHED, "x")
    row = client.get("/api/setups?symbol=TEST").json()["setups"][0]
    assert row["state"] == "PROTECTED_LEVEL_BREACHED" and row["display"] == "INVALIDATED"
    hub.stop_poller()


def test_paper_button_flow_never_bypasses_gate_and_live_stays_off(tmp_path, stubbed):
    pytest.importorskip("MetaTrader5")  # placement converts via broker constants
    StubAnalyzer.candidates = [CausalCandidate(setup=REAL_SETUP, setup_time=BASE,
                                               sweep=REAL_SWEEP, csd=REAL_CSD)]
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    client.get("/api/analysis/TEST/M15")
    st = client.get("/api/status").json()
    assert st["live_execution_enabled"] is False and st["paper_enabled"] is True
    placed = client.post("/api/paper/SET-REAL").json()
    assert placed["status"] == "ORDER_PLACED"
    assert all(r.get("action") in (5, 8) for r in src.sent)  # PENDING/REMOVE only
    # untracked symbol is rejected by the paper gate itself
    gate_msg = hub._paper_gate({"action": "pending", "symbol": "BTCUSD",
                                "type": "BUY_LIMIT", "comment": "SMCGUI-x"})
    assert "not authorized" in gate_msg
    # unregistered setup cannot be placed at all
    with pytest.raises(ValueError):
        hub.paper_place("SET-UNKNOWN")
    assert client.get("/api/status").json()["live_execution_enabled"] is False
    hub.stop_poller()


def test_no_ui_facing_layer_can_reach_order_send_directly():
    """Static guarantee: api.py and the static frontend contain no order_send
    call; only the MT5 adapter definition and the hub's gated paper functions
    may reference it."""
    for p in [Path("src/smc_engine/web/api.py"),
              Path("src/smc_engine/web/static/app.js"),
              Path("src/smc_engine/web/static/index.html")]:
        text = p.read_text(encoding="utf-8")
        assert "order_send" not in text, p
    hub_src = Path("src/smc_engine/web/hub.py").read_text(encoding="utf-8")
    tree = ast.parse(hub_src)
    src_lines = hub_src.splitlines()
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "order_send"]
    assert calls
    funcs = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)]
    for call in calls:
        line = src_lines[call.lineno - 1]
        if "self._mt5.order_send" in line:  # adapter definition
            continue
        holder = [f.name for f in funcs if f.lineno <= call.lineno <= f.end_lineno]
        assert any(h in ("paper_place", "paper_cancel") for h in holder), \
            f"order_send outside gated paper functions at line {call.lineno}"
