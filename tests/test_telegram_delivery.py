"""Phase 14C — Telegram delivery: multi-symbol routing, idempotency, recovery.

Deterministic fixtures; the Telegram transport is mocked (no real messages).
These tests exercise the COHERENT production path:
    background scan -> authoritative event -> persistence -> dispatch -> Telegram.
"""
from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

import src.smc_engine.telegram as tg_mod
from src.smc_engine.telegram import TelegramNotifier, _MAX_RETRIES
from src.smc_engine.web.hub import EngineHub
from tests.test_api import FakeDemoSource
from tests.test_opportunity_watcher import prime_timeline, prime_flat

SYMS = ["GBPUSD", "EURUSD", "XAUUSD"]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def sends(monkeypatch):
    """Capture outbound sends; never touches the network."""
    captured: list[str] = []

    def cap(token, chat_id, text, timeout=10.0):
        captured.append(text)
        return {"ok": True}

    monkeypatch.setattr(tg_mod, "_send_telegram_message", cap)
    return captured


def _make_hub(tmp_path, symbols=SYMS, cut="2026-01-26 01:00"):
    # A synthetic credential file in the DB directory makes the hub's notifier
    # ENABLED without depending on the developer's real telegram.txt (which is
    # absent on CI).
    (tmp_path / "telegram.txt").write_text('Token = "TESTTOKEN";\nChatID = "1";\n',
                                           encoding="utf-8")
    src = FakeDemoSource()
    for s in symbols:
        prime_timeline(src, s, cut=cut)
    hub = EngineHub(src, db_path=str(tmp_path / "gui.db"),
                    setup_store_path=str(tmp_path / "state.db"))
    hub.stop_poller()
    hub.telegram._min_send_interval = 0.0     # deterministic: no 1s pacing in tests
    return hub, src


def _drain(hub):
    hub.telegram._queue.join()
    time.sleep(0.2)
    hub.telegram._queue.join()


def _symbols_in(sends):
    def sym(t):
        lines = t.split("\n")
        return lines[2].strip() if len(lines) > 2 else "?"
    return Counter(sym(t) for t in sends)


# ── 1-6: multi-symbol behaviour ───────────────────────────────────────────────

def test_distinct_symbols_each_reach_telegram(tmp_path, sends):
    hub, _ = _make_hub(tmp_path)
    hub.scan_universe_once(force=True)
    _drain(hub)
    sent = _symbols_in(sends)
    for s in SYMS:
        assert sent.get(s, 0) >= 1, f"{s} must independently reach Telegram: {sent}"
    assert len(sends) >= len(SYMS)
    hub.stop_poller()


def test_works_with_other_instrument_selected(tmp_path, sends):
    from fastapi.testclient import TestClient
    from src.smc_engine.web.api import create_app
    hub, _ = _make_hub(tmp_path)
    client = TestClient(create_app(hub))
    client.get("/api/analysis/EURUSD/M15")          # UI selects EURUSD
    assert hub.watchlist == ["EURUSD"]
    hub.scan_universe_once(force=True)
    _drain(hub)
    sent = _symbols_in(sends)
    assert sent.get("GBPUSD", 0) >= 1 and sent.get("XAUUSD", 0) >= 1
    assert hub.watchlist == ["EURUSD"]
    hub.stop_poller()


def test_no_ui_browser_still_notifies(tmp_path, sends):
    hub, _ = _make_hub(tmp_path)          # no TestClient, no watchlist at all
    hub.scan_universe_once(force=True)
    _drain(hub)
    assert len(sends) >= len(SYMS)
    hub.stop_poller()


def test_no_hardcoded_gbp_dependency(tmp_path, sends):
    """A universe without GBPUSD must still notify its own symbols."""
    hub, _ = _make_hub(tmp_path, symbols=["EURUSD", "XAUUSD"])
    hub.scan_universe_once(force=True)
    _drain(hub)
    sent = _symbols_in(sends)
    assert sent.get("EURUSD", 0) >= 1 and sent.get("XAUUSD", 0) >= 1
    assert "GBPUSD" not in sent
    hub.stop_poller()


def test_symbol_without_event_produces_no_notification(tmp_path, sends):
    (tmp_path / "telegram.txt").write_text('Token="T";\nChatID="1";\n', encoding="utf-8")
    src = FakeDemoSource()
    prime_timeline(src, "GBPUSD", cut="2026-01-26 01:00")   # eligible -> READY
    prime_flat(src, "EURAUD")                               # flat -> no event
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    hub.stop_poller()
    hub.scan_universe_once(force=True)
    _drain(hub)
    sent = _symbols_in(sends)
    assert sent.get("GBPUSD", 0) >= 1
    assert sent.get("EURAUD", 0) == 0, "no fabricated notification for a flat symbol"
    hub.stop_poller()


def test_symbol_exception_does_not_stop_other_symbols(tmp_path, sends):
    hub, _ = _make_hub(tmp_path)
    orig = hub._causal_view

    def boom(sym):
        if sym == "EURUSD":
            raise RuntimeError("injected per-symbol failure")
        return orig(sym)

    hub._causal_view = boom
    stats = hub.scan_universe_once(force=True)
    _drain(hub)
    sent = _symbols_in(sends)
    assert stats["symbols_failed"] >= 1
    assert sent.get("GBPUSD", 0) >= 1 and sent.get("XAUUSD", 0) >= 1
    hub.stop_poller()


# ── 7-13: duplicate prevention ────────────────────────────────────────────────

def test_repeated_scan_does_not_resend(tmp_path, sends):
    hub, _ = _make_hub(tmp_path)
    hub.scan_universe_once(force=True)
    _drain(hub)
    first = len(sends)
    assert first >= len(SYMS)
    for _ in range(3):
        hub.scan_universe_once(force=True)
        _drain(hub)
    assert len(sends) == first, "repeat scans must not resend delivered events"
    hub.stop_poller()


def test_restart_does_not_resend(tmp_path, sends):
    hub, _ = _make_hub(tmp_path)
    hub.scan_universe_once(force=True)
    _drain(hub)
    first = len(sends)
    hub.stop_poller()
    sends.clear()
    hub2, _ = _make_hub(tmp_path)         # same DB, fresh notifier (restart)
    hub2.scan_universe_once(force=True)
    _drain(hub2)
    assert len(sends) == 0, "restart must not resend completed deliveries"
    hub2.stop_poller()


def test_same_logical_event_once(tmp_path, sends):
    hub, _ = _make_hub(tmp_path, symbols=["GBPUSD"])
    ev = {"kind": "READY", "symbol": "GBPUSD", "opportunity_id": "opp-1",
          "event_id": "READY:123"}
    hub.telegram.notify(ev)
    hub.telegram.notify(dict(ev))         # identical logical event
    _drain(hub)
    assert len(sends) == 1
    assert hub.telegram.diagnostics()["telegram_deduplicated_count"] == 1
    hub.stop_poller()


def test_distinct_lifecycle_events_same_opportunity_delivered(tmp_path, sends):
    hub, _ = _make_hub(tmp_path, symbols=["GBPUSD"])
    hub.telegram.notify({"kind": "READY", "symbol": "GBPUSD",
                         "opportunity_id": "opp-1", "event_id": "READY:123"})
    hub.telegram.notify({"kind": "ENTRY_TRIGGERED", "symbol": "GBPUSD",
                         "opportunity_id": "opp-1", "event_id": "ENTRY_PASSED:123"})
    _drain(hub)
    assert len(sends) == 2, "distinct lifecycle events must both deliver"
    hub.stop_poller()


def test_different_symbols_same_event_id_do_not_collide(tmp_path, sends):
    hub, _ = _make_hub(tmp_path, symbols=["GBPUSD"])
    hub.telegram.notify({"kind": "READY", "symbol": "GBPUSD",
                         "opportunity_id": "opp-g", "event_id": "READY:123"})
    hub.telegram.notify({"kind": "READY", "symbol": "XAUUSD",
                         "opportunity_id": "opp-x", "event_id": "READY:123"})
    _drain(hub)
    assert len(sends) == 2
    assert _symbols_in(sends).get("GBPUSD") == 1
    assert _symbols_in(sends).get("XAUUSD") == 1
    hub.stop_poller()


def test_concurrent_consumers_claim_once(tmp_path, sends):
    hub, _ = _make_hub(tmp_path, symbols=["GBPUSD"])
    ev = {"kind": "READY", "symbol": "GBPUSD", "opportunity_id": "opp-c",
          "event_id": "READY:999"}
    threads = [threading.Thread(target=hub.telegram.notify, args=(dict(ev),))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _drain(hub)
    assert len(sends) == 1, "concurrent dispatchers must claim the event once"
    hub.stop_poller()


def test_legit_later_event_not_suppressed(tmp_path, sends):
    hub, _ = _make_hub(tmp_path, symbols=["GBPUSD"])
    hub.telegram.notify({"kind": "READY", "symbol": "GBPUSD",
                         "opportunity_id": "opp-2", "event_id": "READY:a"})
    hub.telegram.notify({"kind": "INVALIDATED", "symbol": "GBPUSD",
                         "opportunity_id": "opp-2", "event_id": "INVALIDATED:b"})
    _drain(hub)
    assert len(sends) == 2
    hub.stop_poller()


# ── 14-19: failures, recovery, queue, ambiguity, leakage ──────────────────────

def _notifier(tmp_path, monkeypatch, send_impl, min_interval=0.0):
    (tmp_path / "telegram.txt").write_text('Token="T";\nChatID="1";\n', encoding="utf-8")
    monkeypatch.setattr(tg_mod, "_send_telegram_message", send_impl)
    return TelegramNotifier(str(tmp_path / "telegram.txt"), min_send_interval=min_interval)


def test_definitive_urlerror_retries_bounded(tmp_path, monkeypatch):
    import urllib.error
    attempts = [0]

    def impl(token, chat_id, text, timeout=10.0):
        attempts[0] += 1
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    n = _notifier(tmp_path, monkeypatch, impl)
    try:
        n.notify({"kind": "READY", "symbol": "EURUSD", "opportunity_id": "u1",
                  "event_id": "e"}, None)
        n._queue.join()
        d = n.diagnostics()
        assert attempts[0] == _MAX_RETRIES + 1
        assert d["telegram_failure_count"] == 1
    finally:
        n.stop()


def test_timeout_is_uncertain_and_not_retried(tmp_path, monkeypatch):
    attempts = [0]

    def impl(token, chat_id, text, timeout=10.0):
        attempts[0] += 1
        raise TimeoutError("timed out")

    n = _notifier(tmp_path, monkeypatch, impl)
    try:
        n.notify({"kind": "READY", "symbol": "EURUSD", "opportunity_id": "t1",
                  "event_id": "e"}, None)
        n._queue.join()
        d = n.diagnostics()
        assert attempts[0] == 1, "ambiguous timeout must not be retried"
        assert d["telegram_uncertain_count"] == 1
        assert d["telegram_failure_count"] == 1
        assert d["telegram_send_count"] == 0
    finally:
        n.stop()


def test_post_send_parse_error_is_not_retried(tmp_path, monkeypatch):
    """A 2xx with an unparseable body means the message was accepted."""
    import urllib.request

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"<html>not json</html>"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    (tmp_path / "telegram.txt").write_text('Token="T";\nChatID="1";\n', encoding="utf-8")
    n = TelegramNotifier(str(tmp_path / "telegram.txt"), min_send_interval=0.0)
    try:
        n.notify({"kind": "READY", "symbol": "EURUSD", "opportunity_id": "p1",
                  "event_id": "e"}, None)
        n._queue.join()
        d = n.diagnostics()
        assert d["telegram_send_count"] == 1
        assert d["telegram_failure_count"] == 0
    finally:
        n.stop()


def test_failed_event_does_not_block_unrelated(tmp_path, monkeypatch):
    calls = []

    def impl(token, chat_id, text, timeout=10.0):
        calls.append(text)
        if "BAD" in text:
            raise OSError("boom")
        return {"ok": True}

    n = _notifier(tmp_path, monkeypatch, impl)
    try:
        n.notify({"kind": "READY", "symbol": "BAD", "opportunity_id": "b",
                  "event_id": "e"}, None)
        n.notify({"kind": "READY", "symbol": "GOOD", "opportunity_id": "g",
                  "event_id": "e"}, None)
        n._queue.join()
        assert n.diagnostics()["telegram_send_count"] == 1
        assert n.diagnostics()["telegram_by_symbol"].get("GOOD", {}).get("delivered") == 1
    finally:
        n.stop()


def test_queue_overflow_bounded_and_observable(tmp_path):
    (tmp_path / "telegram.txt").write_text('Token="T";\nChatID="1";\n', encoding="utf-8")
    n = TelegramNotifier(str(tmp_path / "telegram.txt"), min_send_interval=0.0)
    n._stop.set(); n._worker_thread.join(timeout=3)     # worker not draining
    for i in range(60):
        n.notify({"kind": "READY", "symbol": "X", "opportunity_id": f"q{i}",
                  "event_id": f"e{i}"}, None)
    d = n.diagnostics()
    assert n._queue.qsize() <= 50
    assert d["telegram_dropped_count"] >= 1
    n.stop()


# ── root-cause regression: engine event identity is stable across scans ───────

def test_engine_event_identity_is_stable_across_scans(tmp_path):
    """The same logical event must keep ONE identity as the scan clock advances.

    Regression for the duplication mechanism: identity derived from the market
    anchor, never the observation clock.
    """
    from pathlib import Path
    from src.smc_engine.opportunity import evaluator
    from src.smc_engine.opportunity.models import OpportunityWindows
    from tests.test_opportunity_engine import CFG, _engine, timeline

    d1, h4, m15 = timeline(entry_touch=True)
    eng, repo = _engine(Path(tmp_path), windows=OpportunityWindows(ready_ttl_bars=192))
    ids = set()
    for t in m15["time"].iloc[-8:]:
        view = evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG)
        res = eng.observe("GBPUSD", view)
        for e in res["events"]:
            if e["kind"] == "ENTRY_TRIGGERED":
                ids.add(e["event_id"])
    assert len(ids) == 1, f"one logical event must have one identity: {ids}"
    n = repo._conn.execute(
        "SELECT COUNT(*) FROM opportunity_events WHERE kind='ENTRY_TRIGGERED'"
    ).fetchone()[0]
    assert n == 1, "advancing scan clock must not mint duplicate event rows"


# ── Phase 14D: shared-instance dedup and expired-signal safety ────────────────

def test_two_instances_shared_db_do_not_double_send(tmp_path, sends):
    """Two application instances on the same DB cannot both notify one event."""
    hub1, _ = _make_hub(tmp_path, symbols=["GBPUSD"])
    hub1.scan_universe_once(force=True)
    _drain(hub1)
    n1 = len(sends)
    assert n1 >= 1
    hub2, _ = _make_hub(tmp_path, symbols=["GBPUSD"])   # fresh instance, same DB
    hub2.scan_universe_once(force=True)
    _drain(hub2)
    assert len(sends) == n1, "shared persistent dedup must prevent a second delivery"
    hub1.stop_poller()
    hub2.stop_poller()


def test_expired_opportunity_not_notified_as_current(tmp_path, sends):
    """Re-scanning after expiry must not re-deliver an already-sent notification."""
    import pandas as pd
    hub, _ = _make_hub(tmp_path, symbols=["GBPUSD"])
    for _ in range(3):                       # let the lifecycle reach steady state
        hub.scan_universe_once(force=True)
        _drain(hub)
    before = list(sends)
    assert before, "fixture must deliver at least one notification"
    hub.opportunities.expire_cycle(now=pd.Timestamp("2030-01-01", tz="UTC"))
    _drain(hub)
    for _ in range(2):
        hub.scan_universe_once(force=True)
        _drain(hub)
    # No logical notification is delivered twice...
    assert len(sends) == len(set(sends)), "a notification was delivered more than once"
    # ...and no message sent before expiry recurs afterwards (no stale re-send).
    for t in before:
        assert sends.count(t) == 1, "an old/expired signal was re-sent as current"
    hub.stop_poller()


def test_delivered_message_identifies_python_source(tmp_path, monkeypatch):
    """Every Python-pipeline notification carries a distinguishable source tag.

    A separate sender (an MQL5 EA) shares the same Telegram bot/chat, so the
    message text must identify its origin.
    """
    from src.smc_engine.telegram import _SOURCE_TAG
    sent = []

    def cap(token, chat_id, text, timeout=10.0):
        sent.append(text)
        return {"ok": True}

    (tmp_path / "telegram.txt").write_text('Token="T";\nChatID="1";\n', encoding="utf-8")
    monkeypatch.setattr(tg_mod, "_send_telegram_message", cap)
    n = TelegramNotifier(str(tmp_path / "telegram.txt"), min_send_interval=0.0)
    try:
        n.notify({"kind": "READY", "symbol": "GBPUSD",
                  "opportunity_id": "o1", "event_id": "e1"}, None)
        n._queue.join()
        assert sent, "a notification must be delivered"
        assert all(_SOURCE_TAG in t for t in sent), "every message must carry the source tag"
    finally:
        n.stop()


def test_ambiguous_urlerror_is_not_retried(tmp_path, monkeypatch):
    """A connection reset after the request may have been sent -> no retry."""
    import urllib.error
    attempts = [0]

    def impl(token, chat_id, text, timeout=10.0):
        attempts[0] += 1
        raise urllib.error.URLError(ConnectionResetError(10054, "reset"))

    n = _notifier(tmp_path, monkeypatch, impl)
    try:
        n.notify({"kind": "READY", "symbol": "GBPUSD",
                  "opportunity_id": "a", "event_id": "e"}, None)
        n._queue.join()
        d = n.diagnostics()
        assert attempts[0] == 1, "ambiguous reset must not be retried"
        assert d["telegram_uncertain_count"] == 1
        assert d["telegram_send_count"] == 0
    finally:
        n.stop()


def test_event_ref_distinguishes_distinct_events(tmp_path, monkeypatch):
    """Distinct events get distinct refs; a true duplicate is suppressed."""
    sent = []

    def cap(token, chat_id, text, timeout=10.0):
        sent.append(text)
        return {"ok": True}

    (tmp_path / "telegram.txt").write_text('Token="T";\nChatID="1";\n', encoding="utf-8")
    monkeypatch.setattr(tg_mod, "_send_telegram_message", cap)
    n = TelegramNotifier(str(tmp_path / "telegram.txt"), min_send_interval=0.0)
    try:
        base = {"kind": "READY", "symbol": "GBPUSD"}
        n.notify(dict(base, opportunity_id="opp-A", event_id="READY:1"), None)
        n.notify(dict(base, opportunity_id="opp-B", event_id="READY:1"), None)  # distinct opp
        n.notify(dict(base, opportunity_id="opp-A", event_id="READY:1"), None)  # true duplicate
        n._queue.join()
        assert len(sent) == 2, "distinct events delivered; true duplicate suppressed"
        assert all("ref " in t for t in sent)
        assert sent[0].split("ref ")[-1] != sent[1].split("ref ")[-1], \
            "distinct events must carry distinct refs"
    finally:
        n.stop()


def test_event_ref_distinguishes_kinds_of_same_opportunity(tmp_path, monkeypatch):
    """CONVERTED_TO_SETUP and EXECUTION_READY of one setup share event_id but
    are distinct events, so they must carry distinct refs."""
    sent = []

    def cap(token, chat_id, text, timeout=10.0):
        sent.append(text)
        return {"ok": True}

    (tmp_path / "telegram.txt").write_text('Token="T";\nChatID="1";\n', encoding="utf-8")
    monkeypatch.setattr(tg_mod, "_send_telegram_message", cap)
    n = TelegramNotifier(str(tmp_path / "telegram.txt"), min_send_interval=0.0)
    try:
        for kind in ("CONVERTED_TO_SETUP", "EXECUTION_READY"):
            n.notify({"kind": kind, "symbol": "GBPUSD",
                      "opportunity_id": "opp-Z", "event_id": "SETUP:S1"}, None)
        n._queue.join()
        assert len(sent) == 2
        refs = {t.split("ref ")[-1] for t in sent}
        assert len(refs) == 2, "same setup, different kind -> distinct refs"
    finally:
        n.stop()


def test_send_is_logged_with_identity_and_no_secrets(tmp_path, monkeypatch, caplog):
    import logging
    sent = []

    def cap(token, chat_id, text, timeout=10.0):
        sent.append(text)
        return {"ok": True}

    (tmp_path / "telegram.txt").write_text(
        'Token="SECRETTOKENXYZ";\nChatID="987654321012";\n', encoding="utf-8")
    monkeypatch.setattr(tg_mod, "_send_telegram_message", cap)
    n = TelegramNotifier(str(tmp_path / "telegram.txt"), min_send_interval=0.0)
    try:
        with caplog.at_level(logging.INFO, logger="src.smc_engine.telegram"):
            n.notify({"kind": "READY", "symbol": "GBPUSD",
                      "opportunity_id": "opp-X", "event_id": "READY:7"}, None)
            n._queue.join()
        logs = " ".join(r.getMessage() for r in caplog.records)
        assert "telegram_send" in logs
        assert "outcome=delivered" in logs
        assert "event_id=READY:7" in logs
        assert "SECRETTOKENXYZ" not in logs, "token must never be logged"
        assert "987654321012" not in logs, "chat id must never be logged"
    finally:
        n.stop()
