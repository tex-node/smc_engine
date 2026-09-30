"""CAUSAL SETUP EVENT HISTORY — TEST FIXTURES ONLY.

Every event persisted here is labeled TEST FIXTURE. None of these setups
originate from real market data; they exercise persistence, deduplication,
expiry, restart durability, symbol isolation, malformed rejection, synthetic
(non-canonical) protection and the read-only surface. No broker primitive is
ever invoked (asserted explicitly).
"""
import importlib
import sqlite3

import pandas as pd
import pytest

from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup
from src.smc_engine.web.history import (CANONICAL_SETUP_ID, STATUS_ACTIVE, STATUS_CLOSED,
                                         STATUS_DETECTED, STATUS_EXPIRED, SetupEventHistory,
                                         canonical_reason, timeframe_from_id)
from tests.test_api import FakeDemoSource, build_client, make_setup

CANON_ID = "SETUP-EURAUD-149-OB-M15-391-BULLISH"      # TEST FIXTURE canonical id
CANON_ID_XAU = "SETUP-XAUUSD-201-OB-M15-88-BEARISH"   # TEST FIXTURE canonical id


def payload(setup_id=CANON_ID, symbol="EURAUD", direction="BULLISH", entry=100.0,
            stop_loss=94.0, take_profit=112.0, risk_percent=1.0, as_of="2026-01-01T10:00:00+00:00",
            evidence=None):
    return {"setup_id": setup_id, "symbol": symbol, "timeframe": "M15", "direction": direction,
            "entry": entry, "stop_loss": stop_loss, "take_profit": take_profit,
            "risk_percent": risk_percent, "rr": 2.0, "as_of": as_of,
            "evidence": {"poi_id": "POI", "sweep": {"id": "SW", "side": "SELL_SIDE"}}
                        if evidence is None else evidence}


@pytest.fixture()
def hist(tmp_path):
    h = SetupEventHistory(str(tmp_path / "hist.db"))
    yield h
    h.close()


def test_canonical_identity_rule():
    assert CANONICAL_SETUP_ID.match(CANON_ID)
    assert canonical_reason(payload()) is None
    assert timeframe_from_id(CANON_ID) == "M15"
    # Gate A synthetic label + generic ids are NOT genuine causal identities
    for bad in ("GATEA-224927", "SETUP-API-TEST", "SETUP-EURAUD-NOTANINDEX-OB-M15",
                "SETUP--1-OB-M15", "random", ""):
        assert canonical_reason(payload(bad)) is not None, bad


def test_creation_insert_then_dedup_update(hist):
    assert hist.observe(payload()) == "inserted"
    first = hist.query()["events"][0]
    assert first["status"] == STATUS_DETECTED and first["observations"] == 1
    fs = first["first_seen_at"]
    # five more identical polls -> still exactly one row, first_seen immutable
    for _ in range(5):
        assert hist.observe(payload()) == "updated"
    row = hist.query()["events"][0]
    assert hist.count() == 1
    assert row["first_seen_at"] == fs          # first_seen preserved
    assert row["status"] == STATUS_ACTIVE      # DETECTED -> ACTIVE on re-observation
    assert row["observations"] == 6


def test_snapshot_is_immutable(hist):
    hist.observe(payload(entry=100.0))
    # later observation with DIFFERENT entry must NOT rewrite the audit snapshot
    hist.observe(payload(entry=999.0, as_of="2026-02-02T00:00:00+00:00"))
    e = hist.query()["events"][0]
    assert e["entry"] == 100.0 and e["as_of"] == "2026-01-01T10:00:00+00:00"


def test_malformed_rejected_no_crash(hist):
    for bad in (
        payload(entry=None), payload(entry=float("nan")), payload(entry="x"),
        payload(stop_loss=None), payload(take_profit=float("nan")),
        {"setup_id": CANON_ID},                          # missing symbol/as_of/evidence
        payload(symbol=""), payload(as_of=""), payload(direction="SIDEWAYS"),
        payload(evidence={}),
    ):
        assert hist.observe(bad).startswith("rejected")
        assert hist.count() == 0


def test_symbol_isolation(hist):
    hist.observe(payload(CANON_ID, "EURAUD"))
    hist.observe(payload(CANON_ID_XAU, "XAUUSD", "BEARISH"))
    aud = hist.query(symbol="EURAUD")["events"]
    xau = hist.query(symbol="XAUUSD")["events"]
    assert [e["setup_id"] for e in aud] == [CANON_ID]
    assert [e["setup_id"] for e in xau] == [CANON_ID_XAU]
    assert hist.query(symbol="euraud.")["events"][0]["setup_id"] == CANON_ID  # normalized


def test_expiry_and_revive(hist):
    hist.observe(payload(CANON_ID, "EURAUD"))
    hist.observe(payload(CANON_ID_XAU, "XAUUSD"))
    expired = hist.expire_absent("EURAUD", set())   # engine no longer produces it
    assert expired == 1
    e = hist.query(setup_id=CANON_ID)["events"][0]
    assert e["status"] == STATUS_EXPIRED
    assert e["close_reason"] == "NO LONGER PRESENT"
    # other symbol untouched
    assert hist.query(setup_id=CANON_ID_XAU)["events"][0]["status"] == STATUS_DETECTED
    # reappears -> revived to ACTIVE, cleared reason, history NOT deleted
    hist.observe(payload(CANON_ID, "EURAUD"))
    e2 = hist.query(setup_id=CANON_ID)["events"][0]
    assert e2["status"] == STATUS_ACTIVE and e2["close_reason"] is None


def test_lifecycle_observation_only_when_present(hist):
    hist.record_lifecycle(CANON_ID, "EXECUTION_READY", "PROTECTED_LEVEL_BREACHED", "x")
    assert hist.query(setup_id=CANON_ID)["events"] == []   # no invented event
    hist.observe(payload())
    hist.record_lifecycle(CANON_ID, "EXECUTION_READY", "PROTECTED_LEVEL_BREACHED", "breach")
    e = hist.query()["events"][0]
    assert e["status"] == STATUS_EXPIRED and e["close_reason"] == "PROTECTED_LEVEL_BREACHED"
    hist.record_lifecycle(CANON_ID, "PROTECTED_LEVEL_BREACHED", "CLOSED", "cancelled")
    # CLOSED only re-labels if not already terminal-expired; timeline still grows
    tl = hist.timeline(CANON_ID)
    assert any(t["kind"] == "STATE" for t in tl)


def test_review_and_paper_observation(hist):
    hist.observe(payload())
    assert hist.mark_reviewed(CANON_ID) is True
    assert hist.mark_reviewed("SETUP-UNKNOWN-1-OB-M15") is False
    hist.mark_reviewed(CANON_ID)   # idempotent reviewed_at, increments count
    e = hist.query()["events"][0]
    assert e["review_count"] == 2 and e["reviewed_at"]
    hist.mark_paper_execution(CANON_ID, "ORDER_PLACED ticket=5001")
    hist.mark_paper_execution(CANON_ID, "CANCELLED ticket=5001", requested=False)
    e = hist.query()["events"][0]
    assert e["paper_execution_requested_at"] and e["paper_execution_outcome"] == "CANCELLED ticket=5001"


def test_restart_persistence(tmp_path):
    p = str(tmp_path / "hist.db")
    h1 = SetupEventHistory(p)
    h1.observe(payload())
    first_seen = h1.query()["events"][0]["first_seen_at"]
    h1.close()
    h2 = SetupEventHistory(p)                      # fresh connection after "restart"
    e = h2.query()["events"][0]
    assert e["setup_id"] == CANON_ID and e["first_seen_at"] == first_seen
    h2.close()


def test_cursor_pagination(hist):
    for i in range(3):
        hist.observe(payload(f"SETUP-EURAUD-{i}-OB-M15-{i}-BULLISH"))
    page1 = hist.query(limit=2)
    assert len(page1["events"]) == 2 and page1["next_cursor"] is not None
    page2 = hist.query(limit=2, before_id=page1["next_cursor"])
    assert {e["setup_id"] for e in page2["events"]}.isdisjoint(
        {e["setup_id"] for e in page1["events"]})


# ---------------- API surface + hub observation (read-only, zero broker) ----------------

def _canonical_registered_setup(sid=CANON_ID, symbol="EURAUD"):
    return TradeSetup(
        id=sid, symbol=symbol, direction=Direction.BULLISH,
        created_time=pd.Timestamp("2026-01-01T10:00:00", tz="UTC"),
        poi_id="P", sweep_id="S", csd_id="C", protected_level=94.0,
        order_block_id="OB-M15-391-BULLISH", inducement_id="I",
        entry=100.0, stop_loss=94.0, take_profit=112.0, irl_swing_id="R",
        invalidation_level=94.0, risk_percent=1.0)


def test_api_readiness_persists_genuine_setup_only(tmp_path):
    client, hub = build_client(tmp_path, FakeDemoSource())
    hub.register_setup(_canonical_registered_setup())
    r = client.get("/api/readiness?symbol=EURAUD")
    assert r.status_code == 200 and r.json()["status"] == "READY_FOR_MANUAL_VALIDATION"
    ev = client.get("/api/setup-history?symbol=EURAUD").json()["events"]
    assert [e["setup_id"] for e in ev] == [CANON_ID]
    assert ev[0]["observations"] >= 1
    hub.stop_poller()


def test_api_non_canonical_never_hits_history(tmp_path):
    client, hub = build_client(tmp_path, FakeDemoSource())
    hub.register_setup(make_setup("SETUP-GENERIC-1"))   # readiness shows it, history rejects it
    assert client.get("/api/setups?symbol=TEST").status_code == 200
    assert client.get("/api/setup-history?symbol=TEST").json()["events"] == []
    hub.stop_poller()


def test_api_no_setup_no_history_row(tmp_path):
    client, hub = build_client(tmp_path, FakeDemoSource())
    client.get("/api/readiness")                     # zero setups
    assert client.get("/api/setup-history").json()["events"] == []
    hub.stop_poller()


def test_api_history_readonly_and_detail(tmp_path):
    client, hub = build_client(tmp_path, FakeDemoSource())
    hub.register_setup(_canonical_registered_setup())
    client.get("/api/readiness?symbol=EURAUD")
    # read-only: GETs do not mutate observation count beyond readiness
    before = client.get(f"/api/setup-history/{CANON_ID}").json()["event"]
    for _ in range(4):
        client.get("/api/setup-history")
    after = client.get(f"/api/setup-history/{CANON_ID}").json()["event"]
    assert before["observations"] == after["observations"]
    # detail carries structured evidence + timeline
    assert isinstance(after["evidence"], dict) and "sweep_id" in after["evidence"]
    assert any(t["kind"] == "DETECTED" for t in after["timeline"])
    # review endpoint marks the audit; never executes
    rev = client.post(f"/api/setup-history/{CANON_ID}/review")
    assert rev.status_code == 200 and rev.json()["review_count"] == 1
    assert client.post("/api/setup-history/SETUP-NOPE-1-OB-M15/review").status_code == 404
    assert client.get("/api/setup-history?status=BOGUS").status_code == 400
    hub.stop_poller()


def test_history_slice_crosses_no_execution_boundary(tmp_path):
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    hub.register_setup(_canonical_registered_setup())
    for _ in range(6):
        client.get("/api/readiness?symbol=EURAUD")
        client.get("/api/setup-history")
    client.post(f"/api/setup-history/{CANON_ID}/review")
    assert src.sent == []            # zero order_send AND zero order_check


def test_history_api_and_module_have_no_broker_primitives():
    api_src = open("src/smc_engine/web/api.py", encoding="utf-8").read()
    hist_src = open("src/smc_engine/web/history.py", encoding="utf-8").read()
    js = open("src/smc_engine/web/static/app.js", encoding="utf-8").read()
    for token in ("order_send", "order_check", "TRADE_ACTION"):
        assert token not in hist_src
        assert token not in api_src
    for token in ("order_send", "TRADE_ACTION"):
        assert token not in js
    # history module has no import of MetaTrader5 or execution adapters
    assert "MetaTrader5" not in hist_src
    assert "execution_policy" not in hist_src.lower()
