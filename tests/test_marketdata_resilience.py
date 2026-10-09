"""Market-data resilience slice tests.

All broker interaction is a fake in-process source; tests assert zero order
primitives. Fixtures here are TEST FIXTURES, never production signals.
"""
import re
from pathlib import Path

import pandas as pd
import pytest

import src.smc_engine.web.hub as hub_mod
from src.smc_engine.web.hub import DictSource, EngineHub, TIMEFRAMES
from src.smc_engine.web.api import create_app
from fastapi.testclient import TestClient

from tests.test_api import build_client

BASE = pd.Timestamp("2026-01-01", tz="UTC")


def bars_df(close=100.0, n=120):
    return pd.DataFrame([[BASE + pd.Timedelta(hours=i), close, close + 0.5,
                          close - 0.5, close] for i in range(n)],
                        columns=["time", "open", "high", "low", "close"])


class TickSource(DictSource):
    """Prime bars so history succeeds; control tick stream behavior precisely."""

    def __init__(self, zero_attempts=0, tick_none=False):
        super().__init__()
        self._n = 0
        self.zero_attempts = zero_attempts
        self.tick_none = tick_none
        self.tick_calls = 0

    def tick(self, symbol):
        self.tick_calls += 1
        self._n += 1
        if self.tick_none:
            return None
        if self._n <= self.zero_attempts:
            return {"bid": 0.0, "ask": 0.0, "time": 0}
        return {"bid": 1.12345, "ask": 1.12351, "time": 123}


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(hub_mod, "QUOTE_RETRY_DELAY", 0.0)


def _analysis(src, tmp_path, tag="a"):
    hub = EngineHub(src, db_path=str(tmp_path / f"{tag}g.db"),
                    setup_store_path=str(tmp_path / f"{tag}s.db"))
    try:
        return hub.analysis("TEST", "M15", 60), hub
    finally:
        hub.stop_poller()


def test_zero_tick_then_valid_on_retry_is_AVAILABLE(tmp_path):
    src = TickSource(zero_attempts=2)
    src.prime("TEST", TIMEFRAMES["M15"], bars_df())
    src.prime("TEST", TIMEFRAMES["H4"], bars_df())
    src.prime("TEST", TIMEFRAMES["D1"], bars_df())
    a, _ = _analysis(src, tmp_path, "t1")
    assert a["quote"]["market_data"] == "AVAILABLE"
    assert a["quote"]["bid"] == 1.12345
    assert src.tick_calls == 3            # finite bounded retries, not a loop storm


def test_persistent_zero_tick_is_WAITING_FOR_LIVE_TICK(tmp_path):
    src = TickSource(zero_attempts=10_000)
    src.prime("TEST", TIMEFRAMES["M15"], bars_df())
    src.prime("TEST", TIMEFRAMES["H4"], bars_df())
    src.prime("TEST", TIMEFRAMES["D1"], bars_df())
    a, _ = _analysis(src, tmp_path, "t2")
    assert a["quote"]["market_data"] == "WAITING_FOR_LIVE_TICK"
    assert a["quote"]["bid"] is None and a["quote"]["ask"] is None  # no fabricated price
    assert src.tick_calls == hub_mod.QUOTE_ATTEMPTS                 # bounded
    assert len(a["candles"]) > 0                                    # history still served


def test_no_tick_object_at_all_is_UNAVAILABLE(tmp_path):
    src = TickSource(tick_none=True)
    src.prime("TEST", TIMEFRAMES["M15"], bars_df())
    src.prime("TEST", TIMEFRAMES["H4"], bars_df())
    src.prime("TEST", TIMEFRAMES["D1"], bars_df())
    a, _ = _analysis(src, tmp_path, "t3")
    assert a["quote"]["market_data"] == "UNAVAILABLE"


def test_api_503_when_history_missing_is_fetch_error_surface(tmp_path):
    client, hub = build_client(tmp_path)          # DictSource with only TEST primed
    r = client.get("/api/analysis/NOPE/M15")
    assert r.status_code == 503
    hub.stop_poller()


def test_identity_is_derived_and_present_on_analysis_and_status(tmp_path):
    src = TickSource()
    src.account_info = dict(src.account_info, server="Fake-Demo-9")
    for tf in ("M15", "H4", "D1"):
        src.prime("TEST", TIMEFRAMES[tf], bars_df())

    class DemoTick(TickSource):
        @property
        def is_demo(self): return True

    src2 = DemoTick()
    src2.account_info = dict(src2.account_info, server="Fake-Demo-9")
    for tf in ("M15", "H4", "D1"):
        src2.prime("TEST", TIMEFRAMES[tf], bars_df())
    client = TestClient(create_app(EngineHub(src2, db_path=str(tmp_path / "g.db"),
                                             setup_store_path=str(tmp_path / "s.db"))))
    a = client.get("/api/analysis/TEST/M15").json()
    ident = a["identity"]
    assert ident["account_class"] == "DEMO"          # derived from server name
    assert ident["source"] == "static"
    assert "server" in ident and "login" in ident
    hub_status = client.get("/api/status").json()
    assert hub_status["account_mode"] == "DEMO"
    # non-demo source must yield LIVE, proving nothing is hard-coded
    hub = EngineHub(src, db_path=str(tmp_path / "g2.db"), setup_store_path=str(tmp_path / "s2.db"))
    assert hub._identity()["account_class"] == "LIVE"
    hub.stop_poller()


def test_identity_never_hardcoded_demo_on_live_like_source(tmp_path):
    src = TickSource()
    src.account_info = dict(src.account_info, server="SomeBroker-Real")
    for tf in ("M15", "H4", "D1"):
        src.prime("TEST", TIMEFRAMES[tf], bars_df())
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"), setup_store_path=str(tmp_path / "s.db"))
    ident = hub._identity()
    assert ident["account_class"] == "LIVE"          # derived from is_demo, not constant
    a = hub.analysis("TEST", "M15", 60)
    assert a["identity"]["account_class"] == "LIVE"
    hub.stop_poller()


def test_symbol_switch_never_carries_previous_quote(tmp_path):
    src = TickSource()
    for sym, cl in (("EURUSD", 1.08), ("USDJPY", 150.0)):
        for tf in ("M15", "H4", "D1"):
            src.prime(sym, TIMEFRAMES[tf], bars_df(cl))
    # make tick return per-symbol values
    def tick(symbol):
        closes = {"EURUSD": 1.08, "USDJPY": 150.0}
        return {"bid": closes[symbol], "ask": closes[symbol] + 0.001, "time": 1}
    src.tick = tick
    hub = EngineHub(src, db_path=str(tmp_path / "xg.db"),
                    setup_store_path=str(tmp_path / "xs.db"))
    try:
        a1 = hub.analysis("EURUSD", "M15", 60)
        a2 = hub.analysis("USDJPY", "M15", 60)
    finally:
        hub.stop_poller()
    assert a1["quote"]["bid"] == 1.08
    assert a2["quote"]["bid"] == 150.0
    assert a1["symbol"] == "EURUSD" and a2["symbol"] == "USDJPY"


# ---------------- static frontend/JS assertions ----------------

def test_visibilitychange_refresh_present_and_immediate():
    js = Path("src/smc_engine/web/static/app.js").read_text(encoding="utf-8")
    assert "visibilitychange" in js
    m = re.search(r"visibilitychange[^\n]*\n[^\n]*", js)
    assert m and "refreshAll" in js
    assert "document.hidden" in js
    assert "function refreshAll" in js and "loadStatus" in js


def test_stale_response_protection_in_loadAnalysis():
    js = Path("src/smc_engine/web/static/app.js").read_text(encoding="utf-8")
    assert "analysisSeq" in js
    # guard now also rejects a response whose symbol/symbolSeq changed mid-flight
    assert re.search(r"if \(my !== analysisSeq", js)


def test_frontend_renders_all_four_states_distinctly():
    js = Path("src/smc_engine/web/static/app.js").read_text(encoding="utf-8")
    assert "WAITING FOR LIVE TICK" in js
    assert "MARKET DATA REQUEST FAILED" in js
    assert "MARKET DATA UNAVAILABLE" in js
    assert '"AVAILABLE"' in js or "=== \"AVAILABLE\"" in js
    # waiting state must not use the error (down) style
    assert 'class="q wait"' in js


def test_quote_and_identity_code_has_no_broker_primitives():
    src_txt = Path("src/smc_engine/web/hub.py").read_text(encoding="utf-8")
    i = src_txt.find("def _quote_block")
    j = src_txt.find("def candidates_for")
    seg = src_txt[i:j]
    for tok in ("order_send", "order_check", "TRADE_ACTION", "symbol_select", "positions_get"):
        assert tok not in seg, tok
