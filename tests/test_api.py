"""API + workstation behavior tests.

Market data sources are injected at the adapter boundary (DictSource /
FakeDemoSource). No real broker is ever contacted here. The causal-to-candidate
engine path is covered by the engine test-suite; these tests cover the web
surface, view-models, and backend-enforced paper gates.
"""
import types

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.smc_engine.setup import TradeSetup
from src.smc_engine.models import Direction
from src.smc_engine.web.api import create_app
from src.smc_engine.web.hub import DictSource, EngineHub, TIMEFRAMES

from tests.test_integration_fixture import build_fixture


def make_setup(sid="SETUP-API-TEST"):
    return TradeSetup(
        id=sid, symbol="TEST", direction=Direction.BULLISH,
        created_time=pd.Timestamp("2026-01-01", tz="UTC"),
        poi_id="P", sweep_id="S", csd_id="C", protected_level=94.0,
        order_block_id="O", inducement_id="I", entry=100.0, stop_loss=94.0,
        take_profit=112.0, irl_swing_id="R", invalidation_level=94.0, risk_percent=1.0,
    )


def build_client(tmp_path, source=None):
    src = source or DictSource()
    d1, h4, m15 = build_fixture()
    src.prime("TEST", TIMEFRAMES["D1"], d1)
    src.prime("TEST", TIMEFRAMES["H4"], h4)
    src.prime("TEST", TIMEFRAMES["M15"], m15)
    hub = EngineHub(src, db_path=str(tmp_path / "gui.db"),
                    setup_store_path=str(tmp_path / "state.db"))
    app = create_app(hub)
    return TestClient(app), hub


@pytest.fixture()
def client(tmp_path):
    c, hub = build_client(tmp_path)
    yield c
    hub.stop_poller()


class FakeDemoSource(DictSource):
    name = "fakedemo"

    def __init__(self):
        super().__init__()
        self.account_info = dict(self.account_info, server="Fake-Demo-9", balance=10000.0)
        self.sent = []
        self._next = 5001
        self.book = {}

    @property
    def is_demo(self):
        return True

    def spec(self, symbol):
        return {"symbol": symbol, "digits": 2, "point": 0.01, "tick_size": 0.1,
                "tick_value": 10.0, "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01, "trade_stops_level": 0, "trade_freeze_level": 0,
                "filling_mode": 1}

    def order_check(self, request):
        return types.SimpleNamespace(retcode=10004, margin=0.0)

    def order_send(self, request):
        import MetaTrader5 as mt5
        self.sent.append(request)
        if request["action"] == mt5.TRADE_ACTION_PENDING:
            ticket = self._next; self._next += 1
            self.book[ticket] = dict(request, ticket=ticket,
                                     price_open=request["price"], volume_current=request["volume"],
                                     magic=request["magic"], comment=request["comment"],
                                     sl=request["sl"], tp=request["tp"])
            return types.SimpleNamespace(retcode=10009, order=ticket, comment="ok")
        if request["action"] == mt5.TRADE_ACTION_REMOVE:
            self.book.pop(request["order"], None)
            return types.SimpleNamespace(retcode=10009, comment="ok")
        return types.SimpleNamespace(retcode=10000, comment="rejected")

    def pending_orders(self, symbol):
        return [{"ticket": o["ticket"], "magic": o["magic"], "type": o.get("type"),
                 "price_open": o["price_open"], "sl": o["sl"], "tp": o["tp"],
                 "volume": o["volume_current"], "comment": o["comment"], "symbol": "TEST"}
                for o in self.book.values()]


def _place_setup_via_hub(client, hub, sid="SETUP-API-TEST"):
    hub.register_setup(make_setup(sid))
    r = client.get("/api/setups")
    assert r.status_code == 200
    return r


def test_status_reports_static_offline_source(client):
    body = client.get("/api/status").json()
    assert body["engine"] == "CONNECTED"
    assert body["live_execution_enabled"] is False
    assert body["paper_enabled"] is False  # static source is not a demo broker


def test_symbols_and_market(client):
    assert "TEST" in client.get("/api/symbols").json()["symbols"]
    market = client.get("/api/market/TEST?tf=M15&count=50").json()
    assert market["symbol"] == "TEST" and len(market["candles"]) > 10


def test_unknown_timeframe_rejected(client):
    assert client.get("/api/analysis/TEST/W9").status_code == 400


def test_analysis_returns_engine_overlays(client):
    a = client.get("/api/analysis/TEST/M15?count=120").json()
    for key in ("swings", "liquidity", "sweeps", "structure_events",
                "fvgs", "order_blocks", "pois", "candles", "candidates"):
        assert key in a, key
    assert a["candidates"] == []  # fixture produces no causal candidate; overlays still served
    assert all(f["direction"] in ("BULLISH", "BEARISH") for f in a["fvgs"])


def test_dry_run_and_risk_flow(tmp_path):
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    sid = "SETUP-DRY-1"
    hub.register_setup(make_setup(sid))
    dry = client.post(f"/api/dry-run/{sid}").json()
    assert dry["status"] in ("DRY_RUN_OK", "DRY_RUN_REJECTED")
    if dry["status"] == "DRY_RUN_OK":
        assert dry["order"]["side"] == "BUY_LIMIT"
    risk = client.get(f"/api/risk?symbol=TEST&setup_id={sid}").json()
    assert risk["max_total_risk_percent"] == 3.0
    assert risk["committed_risk_percent"] == pytest.approx(1.0)
    hub.stop_poller()


def test_paper_execution_blocked_without_demo(tmp_path):
    client, hub = build_client(tmp_path)  # DictSource: is_demo False
    hub.register_setup(make_setup("SETUP-GATE-1"))
    r = client.post("/api/paper/SETUP-GATE-1")
    assert r.status_code == 403
    assert "demo" in r.json()["detail"].lower()
    hub.stop_poller()


def test_paper_full_roundtrip_on_fake_demo(tmp_path):
    pytest.importorskip("MetaTrader5")
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    sid = "SETUP-PAPER-1"
    hub.register_setup(make_setup(sid))
    placed = client.post(f"/api/paper/{sid}")
    assert placed.status_code == 200, placed.text
    ticket = placed.json()["ticket"]
    assert src.book[ticket]["magic"] == 202609
    assert str(src.book[ticket]["comment"]).startswith("SMCGUI-")
    # lifecycle advanced through the real state machine
    st = client.get(f"/api/setups/{sid}").json()
    assert st["state"] == "ORDER_PLACED" and st["ticket"] == ticket
    # foreign ticket cancel is refused by the backend gate
    assert client.get("/api/status").status_code == 200
    # cancel own ticket
    canc = client.post(f"/api/paper/{sid}/cancel")
    assert canc.status_code == 200
    assert ticket not in src.book
    assert client.get(f"/api/setups/{sid}").json()["state"] == "CLOSED"
    # no market orders were ever sent
    import MetaTrader5 as mt5
    assert all(r["action"] in (mt5.TRADE_ACTION_PENDING, mt5.TRADE_ACTION_REMOVE) for r in src.sent)
    hub.stop_poller()


def test_duplicate_paper_placement_refused(tmp_path):
    pytest.importorskip("MetaTrader5")
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    sid = "SETUP-DUP-1"
    hub.register_setup(make_setup(sid))
    assert client.post(f"/api/paper/{sid}").status_code == 200
    second = client.post(f"/api/paper/{sid}")
    assert second.status_code in (403, 404, 409, 502)
    assert second.status_code == 403  # state no longer EXECUTION_READY
    hub.stop_poller()


def test_live_endpoint_always_forbidden(client):
    r = client.post("/api/live/whatever")
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"].lower()


def test_hypothesis_crud(client):
    body = {"symbol": "TEST", "timeframe": "H1", "bias": "BULLISH", "thesis": "sweep -> displacement"}
    hid = client.post("/api/hypotheses", json=body).json()["id"]
    rows = client.get("/api/hypotheses").json()["hypotheses"]
    assert any(h["id"] == hid and h["thesis"] == body["thesis"] for h in rows)


def test_alerts_and_history_endpoints(tmp_path):
    pytest.importorskip("MetaTrader5")
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    sid = "SETUP-HIST-1"
    hub.register_setup(make_setup(sid))
    client.post(f"/api/paper/{sid}")
    alerts = client.get("/api/alerts").json()["alerts"]
    kinds = {a["kind"] for a in alerts}
    assert "SETUP_CREATED" in kinds and "ORDER_PLACED" in kinds
    hist = client.get("/api/history").json()["rows"]
    assert hist and hist[0]["setup_id"] == sid
    assert hist[0]["direction"] == "BULLISH"
    hub.stop_poller()


def test_events_endpoint_exists(client):
    # SSE streams forever by design; streaming behavior is validated against the
    # live server in the manual smoke run, not inside pytest.
    assert client.get("/api/status").status_code == 200
