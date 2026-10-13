"""Fix A tests: background-scan setup registration + latest-per-symbol retention
+ registry rehydration. Synthetic fixtures; disposable DBs only."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup
from src.smc_engine.web.hub import EngineHub


def _hub(tmp_path, name="gui.db", state="state.db"):
    from tests.test_api import FakeDemoSource
    # Telegram isolation, identical to test_telegram_delivery._make_hub:
    #   (a) a synthetic credential file in the DB directory shadows the
    #       developer's real C:\smc_engine\telegram.txt so the notifier can
    #       never be enabled with a live token;
    #   (b) stop the background poller so no scan runs and no dispatch occurs.
    (tmp_path / "telegram.txt").write_text('Token = "TESTTOKEN";\nChatID = "1";\n',
                                           encoding="utf-8")
    hub = EngineHub(FakeDemoSource(), db_path=str(tmp_path / name),
                    setup_store_path=str(tmp_path / state))
    hub.stop_poller()
    return hub


def _setup(sid, symbol="AUDCAD", created=None):
    return TradeSetup(id=sid, symbol=symbol, direction=Direction.BULLISH,
                      created_time=created or pd.Timestamp.now(tz="UTC"),
                      poi_id="P", sweep_id="S", csd_id="C", protected_level=1.0,
                      order_block_id="OB-M15-1-BULLISH", inducement_id="I",
                      entry=1.0, stop_loss=0.9, take_profit=1.2, irl_swing_id="R",
                      invalidation_level=0.9, risk_percent=1.0)


def _df(rows=20):
    return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=rows, freq="15min", tz="UTC"),
                         "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0})


def _ids(hub):
    # Retention (latest-per-symbol, 3 days) lives in setups_view(), not
    # lifecycle(), so retention assertions must read through the view.
    return [r["setup_id"] for r in hub.setups_view()]


# ── B1: background scan registers ────────────────────────────────────────────

def test_background_scan_registers_setup(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    setup = _setup("SETUP-AUDCAD-100")
    prov = SimpleNamespace(funnel_events=[], ready_setups=[setup])
    monkeypatch.setattr(hub, "bars", lambda symbol, tf, n: _df())
    monkeypatch.setattr("src.smc_engine.causal.CausalMTFAnalyzer.trace_chain",
                        lambda self, d1, h4, m15, as_of=None: prov)
    hub._trace_funnel("AUDCAD")
    assert "SETUP-AUDCAD-100" in _ids(hub)


def test_background_scan_registration_is_idempotent(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    setup = _setup("SETUP-AUDCAD-101")
    prov = SimpleNamespace(funnel_events=[], ready_setups=[setup])
    monkeypatch.setattr(hub, "bars", lambda symbol, tf, n: _df())
    monkeypatch.setattr("src.smc_engine.causal.CausalMTFAnalyzer.trace_chain",
                        lambda self, d1, h4, m15, as_of=None: prov)
    hub._trace_funnel("AUDCAD")
    hub._trace_funnel("AUDCAD")          # same setup_id again
    assert _ids(hub).count("SETUP-AUDCAD-101") == 1
    assert hub.store.get("SETUP-AUDCAD-101") is not None


# ── B2: retention (latest-per-symbol, 3 days) ────────────────────────────────

def test_latest_per_symbol_evicts_older(tmp_path):
    hub = _hub(tmp_path)
    hub.register_setup(_setup("SETUP-AUDCAD-OLD", "AUDCAD",
                              created=pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=2)))
    hub.register_setup(_setup("SETUP-AUDCAD-NEW", "AUDCAD", created=pd.Timestamp.now(tz="UTC")))
    ids = _ids(hub)
    assert ids == ["SETUP-AUDCAD-NEW"]
    # other symbols are independent
    hub.register_setup(_setup("SETUP-XAUUSD-1", "XAUUSD"))
    assert set(_ids(hub)) == {"SETUP-AUDCAD-NEW", "SETUP-XAUUSD-1"}


def test_older_than_3_days_filtered(tmp_path):
    hub = _hub(tmp_path)
    hub.register_setup(_setup("SETUP-AUDCAD-STALE", "AUDCAD",
                              created=pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=4)))
    assert _ids(hub) == []


# ── B3: rehydration on restart ───────────────────────────────────────────────

def test_rehydration_on_restart(tmp_path):
    hub1 = _hub(tmp_path)
    hub1.register_setup(_setup("SETUP-AUDCAD-PERSIST", "AUDCAD"))
    assert "SETUP-AUDCAD-PERSIST" in _ids(hub1)
    hub1.store.close()
    hub2 = _hub(tmp_path)                # simulated restart, same store
    assert "SETUP-AUDCAD-PERSIST" in _ids(hub2)


def test_rehydration_respects_retention(tmp_path):
    hub1 = _hub(tmp_path)
    hub1.register_setup(_setup("SETUP-AUDCAD-FRESH", "AUDCAD", created=pd.Timestamp.now(tz="UTC")))
    hub1.register_setup(_setup("SETUP-AUDCAD-STALE2", "AUDCAD",
                               created=pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=5)))
    hub1.store.close()
    hub2 = _hub(tmp_path)
    ids = _ids(hub2)
    assert "SETUP-AUDCAD-FRESH" in ids and "SETUP-AUDCAD-STALE2" not in ids


# ── B2: view filtering never mutates the DB ──────────────────────────────────

def test_view_filtering_does_not_modify_db(tmp_path):
    hub = _hub(tmp_path)
    hub.register_setup(_setup("SETUP-AUDCAD-KEEP", "AUDCAD"))
    hub.register_setup(_setup("SETUP-AUDCAD-OLD2", "AUDCAD",
                              created=pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=9)))
    before = len(hub.store.all_setups())
    for _ in range(5):
        hub.lifecycle()
    after = len(hub.store.all_setups())
    assert before == after == 2          # DB retains the audit trail; view filtered
