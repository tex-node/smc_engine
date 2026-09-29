"""End-to-end GUI smoke test against the in-process workstation stack.

Chain: API starts -> GUI loads -> demo source detected -> symbols load ->
market data -> analysis -> setup renders (via hub registration) -> risk ->
execution state. No real broker, no network server.
"""
import pandas as pd

from src.smc_engine.setup import TradeSetup
from src.smc_engine.models import Direction
from src.smc_engine.web.api import create_app
from src.smc_engine.web.hub import DictSource, EngineHub, TIMEFRAMES
from fastapi.testclient import TestClient
from tests.test_api import FakeDemoSource


def test_gui_smoke(tmp_path):
    import pytest
    pytest.importorskip("MetaTrader5")
    src = FakeDemoSource()
    base = pd.Timestamp("2026-01-01", tz="UTC")
    rows = [[base + pd.Timedelta(hours=i), 100, 101, 99, 100] for i in range(60)]
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    src.prime("TEST", TIMEFRAMES["D1"], df)
    src.prime("TEST", TIMEFRAMES["H4"], df)
    src.prime("TEST", TIMEFRAMES["M15"], df)
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"), setup_store_path=str(tmp_path / "s.db"))
    client = TestClient(create_app(hub))

    # API starts + status
    status = client.get("/api/status").json()
    assert status["mt5"] == "CONNECTED" and status["account_mode"] == "DEMO"
    assert status["live_execution_enabled"] is False

    # GUI loads (index + assets served)
    page = client.get("/")
    assert page.status_code == 200 and "SMC" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    # static path traversal blocked
    assert client.get("/static/../../pyproject.toml").status_code in (404, 400)

    # symbols / market / analysis
    assert "TEST" in client.get("/api/symbols").json()["symbols"]
    mkt = client.get("/api/market/TEST?tf=M15").json()
    assert len(mkt["candles"]) == 60
    a = client.get("/api/analysis/TEST/M15").json()
    assert a["timeframe"] == "M15"

    # setup renders
    hub.register_setup(TradeSetup(
        id="SETUP-SMOKE", symbol="TEST", direction=Direction.BULLISH,
        created_time=base, poi_id="P", sweep_id="S", csd_id="C", protected_level=94.0,
        order_block_id="O", inducement_id="I", entry=100.0, stop_loss=94.0,
        take_profit=112.0, irl_swing_id="R", invalidation_level=94.0, risk_percent=1.0))
    setups = client.get("/api/setups").json()["setups"]
    assert setups and setups[0]["display"] == "EXECUTION_READY"

    # risk renders from RiskEngine output
    risk = client.get("/api/risk?symbol=TEST&setup_id=SETUP-SMOKE").json()
    assert risk["equity"] == 10000.0
    assert risk["new_setup"]["volume"] > 0
    assert risk["portfolio_allocation"] == "FIT"

    # execution state renders
    ex = client.get("/api/execution/SETUP-SMOKE").json()
    assert ex["mode"] == "DRY RUN" and ex["account_mode"] == "DEMO"

    # paper place -> lifecycle ORDER_PLACED
    placed = client.post("/api/paper/SETUP-SMOKE").json()
    assert placed["status"] == "ORDER_PLACED"
    assert client.get("/api/lifecycle").json()["states"][0]["display"] == "ORDER_PLACED"

    # error states surface, live stays forbidden
    assert client.get("/api/execution/NOPE").status_code == 404
    assert client.post("/api/live/SETUP-SMOKE").status_code == 403
    hub.stop_poller()
