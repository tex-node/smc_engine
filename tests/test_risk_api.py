"""Risk-API resilience slice tests (GATE B production execution/readiness audit).

TEST FIXTURES ONLY — fake in-process sources; every test asserts zero broker
execution calls. Covers: structured risk results, API/RiskEngine equivalence,
the MT5 module-vs-adapter wiring regression, TRIGGERED admission consumption,
endpoint identity consistency, and UI risk-gating statics.
"""
import re
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import src.smc_engine.web.hub as hub_mod
from src.smc_engine.market import SymbolSpec
from src.smc_engine.models import Direction
from src.smc_engine.risk import RiskEngine
from src.smc_engine.setup import TradeSetup
from src.smc_engine.execution_policy import ExecutionPolicy
from src.smc_engine.web.api import create_app
from src.smc_engine.web.hub import EngineHub, MT5Source

from tests.test_api import FakeDemoSource, build_client

SID = "SETUP-TEST-149-OB-M15-391-BULLISH"     # canonical-shape TEST FIXTURE


def setup(entry=100.0, stop=94.0, tp=112.0, sid=SID, created=None):
    return TradeSetup(
        id=sid, symbol="TEST", direction=Direction.BULLISH,
        created_time=created or pd.Timestamp("2026-01-01", tz="UTC"),
        poi_id="P", sweep_id="S", csd_id="C", protected_level=stop,
        order_block_id="OB-M15-391-BULLISH", inducement_id="I",
        entry=entry, stop_loss=stop, take_profit=tp, irl_swing_id="R",
        invalidation_level=stop, risk_percent=1.0)


@pytest.fixture()
def env(tmp_path):
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    hub.register_setup(setup())
    yield client, hub, src
    hub.stop_poller()


# ---------- TASK 3: structured deterministic results ----------

def test_risk_endpoint_success_is_structured(env):
    client, hub, src = env
    r = client.get(f"/api/risk?symbol=TEST&setup_id={SID}").json()
    assert r["status"] == "RISK_OK"
    for key in ("setup_id", "account_identity", "equity", "requested_risk",
                "stop_distance", "computed_volume", "estimated_loss",
                "committed_portfolio_risk", "available_portfolio_risk",
                "portfolio_gate", "validation"):
        assert key in r, key
    assert r["computed_volume"] > 0 and r["portfolio_gate"] == "FIT"
    assert r["validation"]["ok"] is True
    assert r["account_identity"]["server"] == "Fake-Demo-9"   # derived, not hardcoded
    assert r["account_identity"]["account_class"] == "DEMO"
    # legacy keys preserved for existing consumers
    assert r["new_setup"]["volume"] == r["computed_volume"]
    assert r["portfolio_allocation"] == "FIT"
    assert src.sent == []


def test_api_volume_equals_direct_risk_engine(env):
    client, hub, src = env
    api = client.get(f"/api/risk?symbol=TEST&setup_id={SID}").json()
    lc = hub.registry.get(SID)
    re_ = hub.risk_engine("TEST")
    order = ExecutionPolicy(re_).build_order(lc.setup, 10000.0)
    direct = re_.volume_for_risk(10000.0, lc.setup.risk_percent,
                                 order.entry, order.stop_loss)
    assert api["computed_volume"] == order.volume == direct.volume
    assert abs(api["stop_distance"] - abs(order.entry - order.stop_loss)) < 1e-9


def test_compute_failure_returns_structured_error_not_500(env, monkeypatch):
    client, hub, src = env
    monkeypatch.setattr(ExecutionPolicy, "build_order",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("broker plumbing exploded")))
    r = client.get(f"/api/risk?symbol=TEST&setup_id={SID}")
    assert r.status_code == 200           # never a bare 500 for domain errors
    body = r.json()
    assert body["status"] == "RISK_UNAVAILABLE"
    assert "broker plumbing exploded" in body["compute_error"]
    assert body["validation"]["ok"] is False
    assert body["computed_volume"] is None
    assert src.sent == []


def test_missing_metadata_returns_RISK_UNAVAILABLE(tmp_path):
    src = FakeDemoSource()
    src.spec = lambda symbol: None
    client, hub = build_client(tmp_path, src)
    hub.register_setup(setup())
    body = client.get(f"/api/risk?symbol=TEST&setup_id={SID}").json()
    assert body["status"] == "RISK_UNAVAILABLE"
    assert body["computed_volume"] is None
    hub.stop_poller()


def test_cross_symbol_still_rejected_409(env):
    client, hub, src = env
    r = client.get(f"/api/risk?symbol=XAUUSD&setup_id={SID}")
    assert r.status_code == 409
    assert "not XAUUSD" in r.json()["detail"]


# ---------- root-cause regression: module-vs-adapter wiring ----------

def test_risk_engine_receives_mt5_module_not_adapter(tmp_path):
    fake_mod = __import__("types").SimpleNamespace(
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1,
        order_calc_profit=lambda t, s, v, e, sl: -600.0 * float(v))
    src = MT5Source()
    src._mt5 = fake_mod
    src._connected = True
    src.spec = lambda symbol: {
        "symbol": symbol, "digits": 2, "point": 0.01, "tick_size": 0.1,
        "tick_value": 10.0, "volume_min": 0.01, "volume_max": 100.0,
        "volume_step": 0.01, "trade_stops_level": 0, "trade_freeze_level": 0,
        "filling_mode": 1}
    hub = EngineHub(src, db_path=str(tmp_path / "w.db"),
                    setup_store_path=str(tmp_path / "ws.db"))
    # risk_engine must bind the REAL module (with ORDER_TYPE_* constants)
    eng = hub.risk_engine("TESTUSDX")
    assert eng.mt5 is fake_mod
    # and the exact previous crash (AttributeError 'MT5Source' has no
    # attribute 'ORDER_TYPE_SELL') must now compute instead:
    quote = eng.volume_for_risk(10000.0, 1.0, 1.32, 1.33)   # SELL side
    assert quote.volume > 0
    hub.stop_poller()


# ---------- TASK 5: consumed setups cannot stay EXECUTION_READY ----------

def _bars(rows, base=None):
    base = base or pd.Timestamp("2026-01-01", tz="UTC")
    return pd.DataFrame([[base + pd.Timedelta(minutes=15 * i), o, h, l, c]
                         for i, (o, h, l, c) in enumerate(rows)],
                        columns=["time", "open", "high", "low", "close"])


def test_triggered_entry_trade_consumes_execution_ready(tmp_path):
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    hub.register_setup(setup())
    # bar 0 = creation bar; bar 2 trades down through the 100.0 entry
    m15 = _bars([(100, 100.5, 99.9, 100.2), (100.2, 100.3, 100.05, 100.1),
                 (100.1, 100.15, 99.4, 99.5), (99.5, 99.6, 99.3, 99.4)])
    hub._apply_lifecycle_evaluation(SID, m15)
    row = hub.lifecycle("TEST")[0]
    assert row["state"] == "FILLED"            # entry traded -> not executable
    assert "entry_traded" in (row["reason"] or "")
    assert client.get(f"/api/setups/{SID}").json()["display"] == "ORDER_PLACED"
    hub.stop_poller()


def test_ambiguous_candle_is_never_left_executable(tmp_path):
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    hub.register_setup(setup())
    # bar1 touches both entry (100) and invalidation (94): AMBIGUOUS
    m15 = _bars([(100.5, 100.6, 99.9, 100.4), (100.2, 100.8, 93.5, 95.0),
                 (95.0, 95.4, 94.9, 95.2)])
    hub._apply_lifecycle_evaluation(SID, m15)
    row = hub.lifecycle("TEST")[0]
    assert row["state"] == "ENTRY_NO_LONGER_VALID"
    assert "ambiguous" in (row["reason"] or "")
    hub.stop_poller()


def test_pending_within_window_stays_execution_ready(tmp_path):
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    hub.register_setup(setup())
    m15 = _bars([(105, 105.5, 104.5, 105.2), (105.2, 105.4, 104.8, 105.0)])
    hub._apply_lifecycle_evaluation(SID, m15)
    assert hub.lifecycle("TEST")[0]["state"] == "EXECUTION_READY"
    hub.stop_poller()


# ---------- TASK 6: identity consistent across endpoints ----------

def test_setup_identity_consistent_across_endpoints(env):
    client, hub, src = env
    setups = client.get("/api/setups?symbol=TEST").json()["setups"]
    assert [s["setup_id"] for s in setups] == [SID]
    ready = client.get("/api/readiness?symbol=TEST").json()
    assert SID in ready["detected_ids"]
    hist = client.get("/api/setup-history?symbol=TEST").json()["events"]
    assert hist and hist[0]["setup_id"] == SID
    life = client.get("/api/lifecycle?symbol=TEST").json()["states"][0]
    assert life["setup_id"] == SID and life["state"] == ready["setup"]["state"]
    risk = client.get(f"/api/risk?symbol=TEST&setup_id={SID}").json()
    assert risk["setup_id"] == SID
    assert src.sent == []


# ---------- TASK 7/9 + safety statics ----------

def test_no_broker_calls_across_full_read_flow(env):
    client, hub, src = env
    for _ in range(5):
        client.get("/api/status"); client.get("/api/readiness")
        client.get("/api/setups"); client.get("/api/setup-history")
        client.get(f"/api/risk?symbol=TEST&setup_id={SID}")
    assert src.sent == []


def test_ui_gates_paper_button_on_risk_state():
    js = Path("src/smc_engine/web/static/app.js").read_text(encoding="utf-8")
    assert "PAPER EXECUTION BLOCKED — RISK CHECK UNAVAILABLE" in js
    m = re.search(r'id="btn-paper".*S\.risk\.for === c\.id.*S\.risk\.state === "OK".*disabled', js)
    assert m, "paper button must be gated on a risk result scoped to the displayed setup"
    assert 'S.risk = { state: "UNAVAILABLE", reason: e.message, for: S.selected || null }' in js
    # and analysis path awaits risk before rendering buttons
    assert "await renderRisk();" in js


def test_route_risk_never_generic_500_path():
    api_src = Path("src/smc_engine/web/api.py").read_text(encoding="utf-8")
    i = api_src.find("def risk(")
    seg = api_src[i:i + 320]
    assert "409" in seg                       # explicit domain mapping
    assert "order_send" not in api_src and "order_check" not in api_src
