"""Phase 16H.6 — durable notification outbox + failure-injection tests.

Uses disposable SQLite databases and a monkeypatched transport. Never contacts
Telegram or any real recipient.
"""
from __future__ import annotations

import io
import socket
import urllib.error

import pytest

from src.smc_engine import telegram
from src.smc_engine.notification_ledger import (DELIVERED, FAILED_PERMANENT,
                                                IN_FLIGHT, NotificationLedger,
                                                PENDING, RETRY_WAIT,
                                                SUPPRESSED_QUARANTINE)
from src.smc_engine.telegram import TelegramNotifier

OPP = "OPP-TEST-1"
KIND = "READY"
EV = "e1"
NID = telegram._notification_id(OPP, KIND, EV)
TEXT = "SMC OPPORTUNITY READY\nGBPUSD"


def _ledger(tmp_path, name="outbox.db"):
    return NotificationLedger(str(tmp_path / name))


def _notifier(ledger, check=None):
    n = TelegramNotifier("", quarantine_check=check, ledger=ledger,
                         min_send_interval=0.0)
    n._enabled = True          # test-only: exercise the durable path
    return n


def _http_error(code, body=None):
    payload = io.BytesIO((body or "").encode("utf-8"))
    return urllib.error.HTTPError("https://api.telegram.org/x", code, "err", {}, payload)


def _enqueue(n, opp=OPP, kind=KIND, ev=EV):
    n.notify({"opportunity_id": opp, "kind": kind, "symbol": "GBPUSD", "event_id": ev})


# ── ledger unit ──────────────────────────────────────────────────────────────

def test_intent_is_durable_and_idempotent(tmp_path):
    led = _ledger(tmp_path)
    assert led.record_intent(NID, OPP, KIND, EV, "GBPUSD", TEXT) is True
    assert led.record_intent(NID, OPP, KIND, EV, "GBPUSD", TEXT) is False
    assert led.get(NID)["status"] == PENDING
    assert led.counts() == {PENDING: 1}
    led.close()


def test_duplicate_event_creation_is_idempotent(tmp_path):
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    _enqueue(n)                                # same (opp, kind, event) again
    assert led.counts() == {PENDING: 1}
    led.close()


def test_stale_inflight_recovered_to_pending(tmp_path):
    led = _ledger(tmp_path)
    led.record_intent(NID, OPP, KIND, EV, "GBPUSD", TEXT)
    assert led.claim(NID) is True
    assert led.get(NID)["status"] == IN_FLIGHT
    assert led.claim(NID) is False             # already IN_FLIGHT
    assert led.recover_stale(0) == 1
    assert led.get(NID)["status"] == PENDING
    led.close()


def test_restart_recovers_pending(tmp_path):
    db = str(tmp_path / "o.db")
    a = NotificationLedger(db)
    a.record_intent(NID, OPP, KIND, EV, "GBPUSD", TEXT)
    a.close()
    b = NotificationLedger(db)                 # simulated restart
    rec = b.claim_next()
    assert rec is not None and rec["notification_id"] == NID
    b.close()


# ── delivery outcomes (monkeypatched transport) ──────────────────────────────

def test_successful_delivery(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda t, c, text, timeout=10.0: (sent.append(text),
                        {"ok": True, "result": {"message_id": 42}})[1])
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    rec = led.get(NID)
    assert rec["status"] == DELIVERED and rec["telegram_message_id"] == "42"
    assert len(sent) == 1
    led.close()


def test_429_schedules_retry_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _http_error(429, '{"parameters":{"retry_after":7}}')))
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    rec = led.get(NID)
    assert rec["status"] == RETRY_WAIT
    assert "429" in rec["last_error"] and rec["next_attempt_at"]
    led.close()


def test_5xx_is_permanent_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(500)))
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    assert led.get(NID)["status"] == FAILED_PERMANENT
    led.close()


def test_invalid_credentials_is_permanent_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(401)))
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    assert led.get(NID)["status"] == FAILED_PERMANENT
    led.close()


def test_timeout_is_ambiguous_never_delivered(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(socket.timeout("t")))
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    rec = led.get(NID)
    assert rec["status"] == RETRY_WAIT and "ambiguous" in rec["last_error"]
    led.close()


def test_connection_refused_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(
                            urllib.error.URLError(ConnectionRefusedError())))
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    assert led.get(NID)["status"] == RETRY_WAIT
    led.close()


def test_retry_exhaustion_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(
                            urllib.error.URLError(ConnectionRefusedError())))
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    for _ in range(telegram._MAX_ATTEMPTS + 1):
        rec = led.claim_next(now="2100-01-01T00:00:00+00:00")
        if rec is None:
            break
        n._dispatch_record(rec)
    assert led.get(NID)["status"] == FAILED_PERMANENT
    led.close()


def test_delivered_record_not_resent_on_recovery(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda t, c, text, timeout=10.0: (sent.append(text), {"ok": True})[1])
    led = _ledger(tmp_path)
    n = _notifier(led)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    led.recover_stale(0)
    assert led.claim_next() is None            # DELIVERED is not claimable
    assert len(sent) == 1
    led.close()


# ── quarantine concurrency ───────────────────────────────────────────────────

def test_quarantine_after_enqueue_suppresses(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda t, c, text, timeout=10.0: (sent.append(text), {"ok": True})[1])
    q = {"v": False}
    led = _ledger(tmp_path)
    n = _notifier(led, check=lambda oid: q["v"])
    _enqueue(n)                                 # queued while NOT quarantined
    q["v"] = True                               # quarantined BEFORE dispatch
    n._dispatch_record(led.claim_next())
    assert led.get(NID)["status"] == SUPPRESSED_QUARANTINE
    assert sent == []
    led.close()


def test_quarantine_fail_closed_on_check_error(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send_telegram_message",
                        lambda t, c, text, timeout=10.0: (sent.append(text), {"ok": True})[1])

    def boom(oid):
        raise RuntimeError("db down")
    led = _ledger(tmp_path)
    n = _notifier(led, check=boom)
    _enqueue(n)
    n._dispatch_record(led.claim_next())
    assert led.get(NID)["status"] == SUPPRESSED_QUARANTINE
    assert sent == []
    led.close()


# ── enqueue-time storage failure ─────────────────────────────────────────────

def test_ledger_unavailable_on_enqueue_fails_closed(tmp_path):
    led = _ledger(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("db down")
    led.record_intent = boom
    n = _notifier(led)
    _enqueue(n)                                 # must not raise
    assert led.counts() == {}
    led.close()


# ── concurrency ──────────────────────────────────────────────────────────────

def test_concurrent_claim_is_exclusive(tmp_path):
    import threading
    led = _ledger(tmp_path)
    led.record_intent(NID, OPP, KIND, EV, "GBPUSD", TEXT)
    won = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        if led.claim(NID):
            won.append(1)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(won) == 1
    led.close()


def test_diagnostics_do_not_expose_message_text(tmp_path):
    led = _ledger(tmp_path)
    led.record_intent(NID, OPP, KIND, EV, "GBPUSD", TEXT)
    diag = led.diagnostics()
    assert diag and "text" not in diag[0]
    led.close()
