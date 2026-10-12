"""Phase 16H.1 — quarantine at the notification boundary.

Deterministic race: an event queued while NOT quarantined must still be
suppressed if the opportunity is quarantined before the outbound send, and an
unrelated opportunity's notification must be unaffected.

Also: fail-closed behaviour when quarantine state cannot be established, and
detection of quarantine storage failures (never silently "not quarantined").
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from src.smc_engine import telegram
from src.smc_engine.opportunity.repository import OpportunityRepository
from src.smc_engine.telegram import TelegramNotifier


def _creds(tmp_path):
    p = tmp_path / "telegram.txt"
    p.write_text('Token = "TESTTOKEN";\nChatID = "1";\n', encoding="utf-8")
    return str(p)


# ── queued-event race (final check immediately before the send) ──────────────

def test_queued_message_suppressed_when_quarantined_before_send(tmp_path, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "_send_telegram_message",
        lambda token, chat, text, timeout=10.0: (sent.append(text), {"ok": True})[1])
    qset: set[str] = set()
    reached = threading.Event()
    release = threading.Event()

    def check(oid):
        if oid == "OPP-A":
            reached.set()
            release.wait(5.0)
        return oid in qset

    n = TelegramNotifier(_creds(tmp_path), min_send_interval=0.0,
                         quarantine_check=check)
    try:
        # 1. queue a legitimate event while NOT quarantined
        n.notify({"opportunity_id": "OPP-A", "kind": "READY", "symbol": "GBPAUD",
                  "event_id": "e1"})
        # 2. the worker reached the send boundary for OPP-A
        assert reached.wait(5.0), "worker never reached the send boundary"
        qset.add("OPP-A")                 # 2. quarantine BEFORE the send
        release.set()
        # 5. a different, eligible opportunity must be delivered
        n.notify({"opportunity_id": "OPP-B", "kind": "READY", "symbol": "GBPUSD",
                  "event_id": "e2"})
        deadline = time.time() + 5.0
        while time.time() < deadline and not any("GBPUSD" in t for t in sent):
            time.sleep(0.02)
    finally:
        release.set()
        n.stop()

    assert not any("GBPAUD" in t for t in sent), "quarantined queued event was delivered"
    assert any("GBPUSD" in t for t in sent), "unrelated event was not delivered"
    assert n.diagnostics()["telegram_quarantined_count"] == 1


# ── notifier fail-closed behaviour ───────────────────────────────────────────

def test_notifier_fails_closed_when_check_raises():
    def boom(oid):
        raise RuntimeError("db unavailable")
    n = TelegramNotifier("", quarantine_check=boom)
    assert n._is_quarantined("OPP-A") is True       # unknown -> suppress


def test_notifier_confirmed_non_quarantined_is_false():
    n = TelegramNotifier("", quarantine_check=lambda oid: False)
    assert n._is_quarantined("OPP-A") is False


def test_notifier_without_check_is_false():
    assert TelegramNotifier("")._is_quarantined("OPP-A") is False


def test_send_suppressed_when_check_raises(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "_send_telegram_message",
        lambda token, chat, text, timeout=10.0: (sent.append(text), {"ok": True})[1])

    def boom(oid):
        raise RuntimeError("db unavailable")
    n = TelegramNotifier("", min_send_interval=0.0, quarantine_check=boom)
    n._enabled = True                                # test-only: exercise send path
    n._send_with_retry({"symbol": "GBPAUD", "kind": "READY", "opp_id": "OPP-A",
                        "ref": "r", "text": "SMC OPPORTUNITY READY\nGBPAUD"})
    assert sent == []
    assert n.diagnostics()["telegram_quarantined_count"] == 1


# ── hub dispatch fail-closed ─────────────────────────────────────────────────

def test_dispatch_fails_closed_when_state_unavailable(tmp_path):
    from src.smc_engine.web.hub import EngineHub
    from tests.test_api import FakeDemoSource
    hub = EngineHub(FakeDemoSource(), db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    calls: list = []

    class FakeTelegram:
        def notify(self, ev, opp):
            calls.append(ev.get("opportunity_id"))

    hub.telegram = FakeTelegram()
    hub.opportunities.repo._conn.execute("DROP TABLE opportunity_quarantine")
    hub.opportunities.repo._conn.commit()
    # must not raise and must not dispatch (never treat unknown as not-quarantined)
    hub._dispatch_opportunity_events({"events": [
        {"opportunity_id": "OPP-A", "kind": "READY", "symbol": "GBPAUD", "message": "x"}]})
    assert calls == []


# ── repository storage failures are surfaced, not silently False ─────────────

def test_missing_quarantine_table_is_detected_not_false(tmp_path):
    repo = OpportunityRepository(str(tmp_path / "r.db"))
    repo._conn.execute("DROP TABLE opportunity_quarantine")
    repo._conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        repo.is_quarantined("OPP-A")
    repo.close()


def test_closed_connection_error_is_surfaced(tmp_path):
    repo = OpportunityRepository(str(tmp_path / "r.db"))
    repo.close()
    with pytest.raises(Exception):
        repo.is_quarantined("OPP-A")
