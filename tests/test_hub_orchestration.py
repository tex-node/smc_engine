"""Orchestration-correctness regression tests for web/hub.py.

Covers the four hardened slices: canonical lifecycle enum, broker-authoritative
ticket accounting (idempotent cancel, foreign tickets, duplicate events),
symbol isolation at the boundary (incl. broker suffix normalization), and the
paper gate's symbol authorization.
"""
import pytest

from src.smc_engine.lifecycle import SetupState
from src.smc_engine.models import SetupState as ReplayState
from src.smc_engine.web.hub import EngineHub, canon_state, norm_symbol

from tests.test_api import FakeDemoSource, build_client, make_setup


# ---------------- canonical enum ----------------
def test_canon_state_lifecycle_enum_identity():
    assert canon_state(SetupState.EXECUTION_READY) is SetupState.EXECUTION_READY


def test_canon_state_string_coercion():
    assert canon_state("ORDER_PLACED") is SetupState.ORDER_PLACED


def test_canon_state_rejects_replay_enum():
    with pytest.raises(ValueError):
        canon_state(ReplayState.TRIGGERED)


def test_canon_state_rejects_unknown_name():
    with pytest.raises(ValueError):
        canon_state("NOT_A_STATE")


def test_invalidation_state_serializes_canonically(tmp_path):
    client, hub = build_client(tmp_path, FakeDemoSource())
    hub.register_setup(make_setup("SET-CANON"))
    hub._transition("SET-CANON", SetupState.PROTECTED_LEVEL_BREACHED, "test breach")
    row = hub.lifecycle("TEST")[0]
    assert row["state"] == "PROTECTED_LEVEL_BREACHED"
    assert row["display"] == "INVALIDATED"
    hub.stop_poller()


# ---------------- symbol normalization / isolation ----------------
def test_norm_symbol_suffix_decoration():
    assert norm_symbol("EURAUD.") == norm_symbol("EURAUD") == norm_symbol("euraud ")


def test_cross_symbol_lifecycle_isolated(tmp_path):
    from src.smc_engine.setup import TradeSetup
    from src.smc_engine.models import Direction
    import pandas as pd
    client, hub = build_client(tmp_path, FakeDemoSource())
    hub.register_setup(make_setup("SET-AUD"))
    other = TradeSetup(
        id="SET-XAU", symbol="XAUUSD", direction=Direction.BEARISH,
        created_time=pd.Timestamp("2026-01-01", tz="UTC"), poi_id="P", sweep_id="S",
        csd_id="C", protected_level=2700.0, order_block_id="O", inducement_id="I",
        entry=2650.0, stop_loss=2700.0, take_profit=2550.0, irl_swing_id="R",
        invalidation_level=2700.0, risk_percent=1.0)
    hub.register_setup(other)
    aud = hub.lifecycle("TEST")
    xau = hub.lifecycle("XAUUSD")
    assert {r["setup_id"] for r in aud} == {"SET-AUD"}
    assert {r["setup_id"] for r in xau} == {"SET-XAU"}
    body = client.get("/api/setups?symbol=TEST").json()["setups"]
    assert all(r["symbol"] == "TEST" for r in body)
    hub.stop_poller()


def test_risk_snapshot_rejects_foreign_symbol_setup(tmp_path):
    client, hub = build_client(tmp_path, FakeDemoSource())
    hub.register_setup(make_setup("SET-MISMATCH"))
    r = client.get("/api/risk?symbol=XAUUSD&setup_id=SET-MISMATCH")
    assert r.status_code == 409
    assert "not XAUUSD" in r.json()["detail"]
    hub.stop_poller()


def test_watch_dedupes_suffixed_symbols(tmp_path):
    hub = EngineHub(FakeDemoSource(), db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    hub.watch("EURAUD")
    hub.watch("euraud.")
    assert len(hub.watchlist) == 1
    hub.stop_poller()


# ---------------- ticket accounting ----------------
def test_duplicate_placement_and_event_one_ticket(tmp_path):
    pytest.importorskip("MetaTrader5")
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    hub.register_setup(make_setup("SET-DUP"))
    first = hub.paper_place("SET-DUP")
    # duplicate placement attempt
    with pytest.raises(PermissionError):
        hub.paper_place("SET-DUP")
    # duplicate event: same target state twice must not publish again
    n_events = len(hub.events.ring)
    applied = hub._transition("SET-DUP", SetupState.ORDER_PLACED, "duplicate replay")
    assert applied is False
    assert len(hub.events.ring) == n_events
    assert src.book[first["ticket"]]["comment"].startswith("SMCGUI-")
    hub.stop_poller()


def test_foreign_ticket_cancel_rejected(tmp_path):
    pytest.importorskip("MetaTrader5")  # paper_place sends via broker constants
    _, hub = build_client(tmp_path, FakeDemoSource())
    hub.register_setup(make_setup("SET-OWN"))
    own = hub.paper_place("SET-OWN")
    assert hub._paper_gate({"action": "remove", "order": own["ticket"]}) is None
    assert hub._paper_gate({"action": "remove", "order": 888888}) == \
        "cancel of ticket not owned by this session"
    hub.stop_poller()


def test_cancel_idempotent_single_broker_send(tmp_path):
    pytest.importorskip("MetaTrader5")
    import MetaTrader5 as mt5
    src = FakeDemoSource()
    _, hub = build_client(tmp_path, src)
    hub.register_setup(make_setup("SET-CANC"))
    placed = hub.paper_place("SET-CANC")
    removes_before = sum(1 for r in src.sent if r.get("action") == mt5.TRADE_ACTION_REMOVE)
    first = hub.paper_cancel("SET-CANC")
    second = hub.paper_cancel("SET-CANC")
    assert first["status"] == "CANCELLED"
    assert second["status"] == "ALREADY_CANCELLED"
    assert second["ticket"] is None
    removes_after = sum(1 for r in src.sent if r.get("action") == mt5.TRADE_ACTION_REMOVE)
    assert removes_after - removes_before == 1  # exactly one broker REMOVE
    assert hub.lifecycle("TEST")[0]["ticket"] is None
    hub.stop_poller()


def test_already_cancelled_at_broker_no_send(tmp_path):
    pytest.importorskip("MetaTrader5")
    src = FakeDemoSource()
    _, hub = build_client(tmp_path, src)
    hub.register_setup(make_setup("SET-GONE"))
    placed = hub.paper_place("SET-GONE")
    ticket = placed["ticket"]
    src.book.pop(ticket)  # external close/expiry at broker
    res = hub.paper_cancel("SET-GONE")
    assert res["status"] == "ALREADY_CANCELLED"
    assert res["ticket"] == ticket
    assert "SET-GONE" not in hub.paper_tickets
    assert hub.lifecycle("TEST")[0]["state"] == "CLOSED"
    import MetaTrader5 as mt5
    assert not [r for r in src.sent if r["action"] == mt5.TRADE_ACTION_REMOVE]
    hub.stop_poller()


def test_paper_gate_symbol_authorization(tmp_path):
    src = FakeDemoSource()
    _, hub = build_client(tmp_path, src)
    hub.register_setup(make_setup("SET-SYM"))
    hub.watch("TEST")
    ok = hub._paper_gate({"action": "pending", "symbol": "TEST.", "type": "BUY_LIMIT",
                          "comment": "SMCGUI-1"})
    assert ok is None  # suffixed symbol matches via normalization
    bad = hub._paper_gate({"action": "pending", "symbol": "NZDJPY", "type": "BUY_LIMIT",
                           "comment": "SMCGUI-1"})
    assert "not authorized" in bad
    hub.stop_poller()
