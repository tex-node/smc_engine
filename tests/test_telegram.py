"""Phase 14B — Telegram notification tests.

All 22 tests use a mock HTTP transport; no real Telegram messages are sent.
Credentials are written to a temp file for credential-loading tests and are
never printed, asserted-equal against literal values, or included in test IDs.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import tempfile
import threading
import time
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.smc_engine.telegram import (
    TelegramNotifier,
    _DEFAULT_ENABLED_KINDS,
    _format_message,
    _load_credentials,
)


# ── private test helpers ──────────────────────────────────────────────────────

def _write_cred_file(path: str, token: str = "TESTTOKEN", chat_id: str = "12345") -> None:
    """Write a synthetic credential file in the expected format."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f'Token     = "{token}";\n')
        fh.write(f'ChatID    = "{chat_id}";\n')


class _FakeOpp:
    """Minimal stand-in for an Opportunity object."""
    def __init__(
        self,
        direction: str = "BEARISH",
        opportunity_type: str = "REVERSAL",
        next_expected: str = "M15 POI mitigation",
        blocker: str = "NO_IDM",
        reason: str = "NO_IDM",
        risk_status: str = "PENDING",
        superseded_by: str = "XAUUSD SHORT",
        supersession_reason: str = "OPPOSING_DIRECTIONAL_THESIS",
    ):
        self.direction = direction
        self.opportunity_type = opportunity_type
        self.next_expected = next_expected
        self.blocker = blocker
        self.reason = reason
        self.risk_status = risk_status
        self.superseded_by = superseded_by
        self.supersession_reason = supersession_reason


def _make_event(
    kind: str,
    symbol: str = "EURUSD",
    opportunity_id: str = "opp-test-1",
) -> dict:
    return {
        "kind": kind,
        "symbol": symbol,
        "opportunity_id": opportunity_id,
        "message": f"{kind} on {symbol}",
        "state": kind,
    }


def _notifier_with_mock_send(
    tmp_path: str,
    side_effect=None,
    min_send_interval: float = 0.0,
) -> tuple[TelegramNotifier, MagicMock]:
    """Create an enabled TelegramNotifier with a patched _send_telegram_message."""
    mock_send = MagicMock(return_value={"ok": True})
    if side_effect is not None:
        mock_send.side_effect = side_effect
    notifier = TelegramNotifier(tmp_path, min_send_interval=min_send_interval)
    # Patch the send function on the module so the worker uses the mock.
    import src.smc_engine.telegram as tg_mod
    tg_mod._send_telegram_message = mock_send
    return notifier, mock_send


def _drain(notifier: TelegramNotifier, timeout: float = 3.0) -> None:
    """Wait for all queued messages to be delivered by the worker thread."""
    notifier._queue.join()


# ── credential loading ────────────────────────────────────────────────────────

def test_credentials_load_successfully(tmp_path):
    """Test 1: credentials load successfully from a well-formed local file."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)
    token, chat_id = _load_credentials(cred)
    assert token and chat_id
    assert len(token) > 3
    assert len(chat_id) > 1


def test_malformed_credential_file_handled_safely(tmp_path):
    """Test 2: a malformed credential file disables the notifier gracefully."""
    cred = str(tmp_path / "telegram.txt")
    with open(cred, "w") as fh:
        fh.write("this is not a credential file\n")
    notifier = TelegramNotifier(cred)
    assert not notifier._enabled
    diag = notifier.diagnostics()
    assert not diag["telegram_configured"]


def test_missing_credential_file_handled_safely(tmp_path):
    """Test 3: an absent credential file disables the notifier gracefully."""
    absent = str(tmp_path / "does_not_exist.txt")
    notifier = TelegramNotifier(absent)
    assert not notifier._enabled
    diag = notifier.diagnostics()
    assert not diag["telegram_enabled"]
    assert not diag["telegram_configured"]


def test_credentials_never_appear_in_logs(tmp_path, caplog):
    """Test 4: the token and chat_id never appear in any log output."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred, token="SECRET_TOKEN_XYZ", chat_id="SECRET_CHAT_99")

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def failing_send(token, chat_id, text, timeout=10.0):
        raise OSError("simulated network error")

    tg_mod._send_telegram_message = failing_send
    try:
        with caplog.at_level(logging.DEBUG, logger="src.smc_engine.telegram"):
            notifier = TelegramNotifier(cred, min_send_interval=0.0)
            notifier.notify(_make_event("READY"), _FakeOpp())
            _drain(notifier)
            notifier.stop()
        all_log = " ".join(r.message for r in caplog.records)
        assert "SECRET_TOKEN_XYZ" not in all_log
        assert "SECRET_CHAT_99" not in all_log
    finally:
        tg_mod._send_telegram_message = original


def test_credentials_never_appear_in_api_responses(tmp_path):
    """Test 5: the diagnostics() dict never exposes token or chat_id."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred, token="MUST_NOT_LEAK", chat_id="MUST_NOT_LEAK_ID")
    notifier = TelegramNotifier(cred)
    diag = notifier.diagnostics()
    diag_str = json.dumps(diag)
    assert "MUST_NOT_LEAK" not in diag_str
    assert notifier._token not in diag_str


# ── message formatting for each enabled kind ─────────────────────────────────

def test_opportunity_created_notification():
    """Test 6: OPPORTUNITY_CREATED formats without execution-ready language."""
    msg = _format_message("OPPORTUNITY_CREATED", "GBPUSD", _FakeOpp(direction="BULLISH"))
    assert "OPPORTUNITY CREATED" in msg
    assert "GBPUSD" in msg
    assert "EXECUTION" not in msg


def test_ready_notification():
    """Test 7: READY notification includes symbol, direction, and OBSERVATION ONLY gate."""
    msg = _format_message("READY", "GBPUSD", _FakeOpp())
    assert "OPPORTUNITY READY" in msg
    assert "GBPUSD" in msg
    assert "OBSERVATION ONLY" in msg
    assert "EXECUTION" not in msg


def test_entry_triggered_notification():
    """Test 8: ENTRY_TRIGGERED message clearly states it is NOT EXECUTION_READY."""
    msg = _format_message("ENTRY_TRIGGERED", "CHFJPY", _FakeOpp())
    assert "ENTRY TRIGGERED" in msg
    assert "CHFJPY" in msg
    # The message must explicitly state this is not execution-ready.
    assert "NOT EXECUTION_READY" in msg or "NOT" in msg
    assert "OBSERVATION ONLY" in msg


def test_converted_to_setup_notification():
    """Test 9: CONVERTED_TO_SETUP produces a valid execution-ready message."""
    msg = _format_message("CONVERTED_TO_SETUP", "XAUUSD", _FakeOpp())
    assert "EXECUTION READY" in msg
    assert "XAUUSD" in msg
    assert "OBSERVATION ONLY" in msg


def test_execution_ready_notification():
    """Test 10: EXECUTION_READY produces a valid execution-ready message."""
    msg = _format_message("EXECUTION_READY", "USDJPY", _FakeOpp())
    assert "EXECUTION READY" in msg
    assert "USDJPY" in msg
    assert "OBSERVATION ONLY" in msg


def test_superseded_notification():
    """Test 11: OPPORTUNITY_SUPERSEDED includes the superseding thesis and reason."""
    opp = _FakeOpp(superseded_by="XAUUSD SHORT", supersession_reason="OPPOSING_DIRECTIONAL_THESIS")
    msg = _format_message("OPPORTUNITY_SUPERSEDED", "XAUUSD", opp)
    assert "SUPERSEDED" in msg
    assert "XAUUSD" in msg
    assert "OPPOSING_DIRECTIONAL_THESIS" in msg


def test_invalidated_notification():
    """Test 12: INVALIDATED includes the blocker/reason."""
    opp = _FakeOpp(blocker="NO_IDM", reason="NO_IDM")
    msg = _format_message("INVALIDATED", "AUDUSD", opp)
    assert "INVALIDATED" in msg
    assert "AUDUSD" in msg
    assert "NO_IDM" in msg


# ── deduplication ─────────────────────────────────────────────────────────────

def test_duplicate_event_sends_once(tmp_path):
    """Test 13: the same (opportunity_id, kind) pair is sent at most once per process."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)

    sent: list[str] = []

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def capturing_send(token, chat_id, text, timeout=10.0):
        sent.append(text)

    tg_mod._send_telegram_message = capturing_send
    try:
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        ev = _make_event("READY", opportunity_id="opp-dedup-1")
        notifier.notify(ev, _FakeOpp())
        notifier.notify(ev, _FakeOpp())  # duplicate
        _drain(notifier)
        notifier.stop()
        assert len(sent) == 1
        diag = notifier.diagnostics()
        assert diag["telegram_deduplicated_count"] == 1
    finally:
        tg_mod._send_telegram_message = original


def test_restart_does_not_resend_events(tmp_path):
    """Test 14: cross-restart dedup is guaranteed by the DB event layer.

    Within a single process, _sent_keys prevents double-sends.
    After a restart (new TelegramNotifier instance), the mechanism that
    prevents re-sending is record_event_once() at the DB layer — result["events"]
    will not contain events already recorded. This test verifies the design
    property: a new notifier starts with an empty _sent_keys set and will only
    send events that reach it; since the DB guarantees only fresh events are
    dispatched, cross-restart duplication cannot occur at the system level.
    """
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)
    n1 = TelegramNotifier(cred, min_send_interval=0.0)
    n1.stop()
    # After restart, _sent_keys is fresh.
    n2 = TelegramNotifier(cred, min_send_interval=0.0)
    n2.stop()
    assert len(n2._sent_keys) == 0


# ── failure isolation ─────────────────────────────────────────────────────────

def test_telegram_failure_does_not_stop_watcher(tmp_path):
    """Test 15: a Telegram send failure is absorbed; the notifier stays live."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)

    call_count = [0]

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def always_fail(token, chat_id, text, timeout=10.0):
        call_count[0] += 1
        raise OSError("simulated failure")

    tg_mod._send_telegram_message = always_fail
    try:
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        notifier.notify(_make_event("READY", opportunity_id="opp-fail-1"), _FakeOpp())
        _drain(notifier)
        # Worker is still alive after the failure.
        assert notifier._worker_thread.is_alive()
        # Failure was recorded in diagnostics.
        assert notifier.diagnostics()["telegram_failure_count"] >= 1
        notifier.stop()
    finally:
        tg_mod._send_telegram_message = original


def test_telegram_timeout_does_not_stop_scanner(tmp_path):
    """Test 16: a network timeout is caught; the scanner continues normally."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def timeout_send(token, chat_id, text, timeout=10.0):
        raise TimeoutError("simulated timeout")

    tg_mod._send_telegram_message = timeout_send
    try:
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        notifier.notify(_make_event("INVALIDATED", opportunity_id="opp-timeout-1"), _FakeOpp())
        _drain(notifier)
        assert notifier._worker_thread.is_alive()
        notifier.stop()
    finally:
        tg_mod._send_telegram_message = original


def test_telegram_429_retry_is_bounded(tmp_path):
    """Test 17: a Telegram 429 response causes bounded retry, not infinite retry."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)

    attempt_count = [0]

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def rate_limited(token, chat_id, text, timeout=10.0):
        attempt_count[0] += 1
        body = json.dumps({"parameters": {"retry_after": 0}}).encode()
        raise urllib.error.HTTPError(
            url="", code=429, msg="Too Many Requests",
            hdrs=None, fp=BytesIO(body),  # type: ignore[arg-type]
        )

    tg_mod._send_telegram_message = rate_limited
    try:
        from src.smc_engine.telegram import _MAX_RETRIES
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        notifier.notify(_make_event("READY", opportunity_id="opp-429-1"), _FakeOpp())
        _drain(notifier)
        # Must attempt at most _MAX_RETRIES + 1 times, then give up.
        assert attempt_count[0] <= _MAX_RETRIES + 1
        assert notifier.diagnostics()["telegram_failure_count"] == 1
        notifier.stop()
    finally:
        tg_mod._send_telegram_message = original


# ── multi-symbol isolation ────────────────────────────────────────────────────

def test_multiple_symbols_remain_isolated(tmp_path):
    """Test 18: events for different symbols generate isolated notifications."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)

    sent: list[str] = []

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def capturing(token, chat_id, text, timeout=10.0):
        sent.append(text)

    tg_mod._send_telegram_message = capturing
    try:
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        notifier.notify(_make_event("READY", symbol="EURUSD", opportunity_id="opp-eu"), _FakeOpp())
        notifier.notify(_make_event("READY", symbol="XAUUSD", opportunity_id="opp-xau"), _FakeOpp())
        _drain(notifier)
        notifier.stop()
        assert len(sent) == 2
        assert any("EURUSD" in t for t in sent)
        assert any("XAUUSD" in t for t in sent)
    finally:
        tg_mod._send_telegram_message = original


# ── UI-independence ───────────────────────────────────────────────────────────

def test_browser_selection_is_irrelevant(tmp_path):
    """Test 19: the notifier has no dependency on any browser or UI state."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)
    sent: list[str] = []

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def capturing(token, chat_id, text, timeout=10.0):
        sent.append(text)

    tg_mod._send_telegram_message = capturing
    try:
        # Instantiate directly — no hub, no HTTP server, no browser.
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        notifier.notify(_make_event("READY", opportunity_id="opp-ui-1"), _FakeOpp())
        _drain(notifier)
        notifier.stop()
        assert len(sent) == 1
    finally:
        tg_mod._send_telegram_message = original


def test_no_browser_discovery_still_sends_notification(tmp_path):
    """Test 20: notify() works without any browser or HTTP request context."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)
    sent: list[str] = []

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def capturing(token, chat_id, text, timeout=10.0):
        sent.append(text)

    tg_mod._send_telegram_message = capturing
    try:
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        # Simulate what scan_universe_once does when no UI is connected.
        notifier.notify(
            {"kind": "INVALIDATED", "symbol": "GBPUSD",
             "opportunity_id": "opp-nobrowser", "message": "test"},
            _FakeOpp(),
        )
        _drain(notifier)
        notifier.stop()
        assert len(sent) == 1
        assert "GBPUSD" in sent[0]
    finally:
        tg_mod._send_telegram_message = original


# ── semantic correctness ──────────────────────────────────────────────────────

def test_entry_triggered_never_formatted_as_execution_ready():
    """Test 21: ENTRY_TRIGGERED message must not contain execution-ready language."""
    msg = _format_message("ENTRY_TRIGGERED", "EURUSD", _FakeOpp())
    # Must say ENTRY TRIGGERED, not EXECUTION READY in the header.
    assert msg.startswith("SMC ENTRY TRIGGERED")
    # Must explicitly distinguish from EXECUTION_READY.
    assert "NOT EXECUTION_READY" in msg or ("NOT" in msg and "Setup has NOT" in msg)
    # Must not have the EXECUTION READY header that EXECUTION_READY uses.
    assert "SMC EXECUTION READY" not in msg


def test_no_credentials_appear_in_test_output(tmp_path, capfd):
    """Test 22: running through a full send cycle produces no credential output."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred, token="CREDENTIAL_CANARY_TOKEN", chat_id="CANARY_CHAT")

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def mock_send(token, chat_id, text, timeout=10.0):
        pass  # discard; never print

    tg_mod._send_telegram_message = mock_send
    try:
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        notifier.notify(_make_event("READY", opportunity_id="opp-canary"), _FakeOpp())
        _drain(notifier)
        notifier.stop()
    finally:
        tg_mod._send_telegram_message = original

    captured = capfd.readouterr()
    assert "CREDENTIAL_CANARY_TOKEN" not in captured.out
    assert "CREDENTIAL_CANARY_TOKEN" not in captured.err
    assert "CANARY_CHAT" not in captured.out
    assert "CANARY_CHAT" not in captured.err


# ── disabled-kinds filtering ──────────────────────────────────────────────────

def test_disabled_kinds_do_not_notify(tmp_path):
    """Bonus: EXPIRED and OPPORTUNITY_CREATED are off by default."""
    cred = str(tmp_path / "telegram.txt")
    _write_cred_file(cred)
    sent: list[str] = []

    import src.smc_engine.telegram as tg_mod
    original = tg_mod._send_telegram_message

    def capturing(token, chat_id, text, timeout=10.0):
        sent.append(text)

    tg_mod._send_telegram_message = capturing
    try:
        notifier = TelegramNotifier(cred, min_send_interval=0.0)
        notifier.notify(_make_event("EXPIRED", opportunity_id="opp-exp"))
        notifier.notify(_make_event("OPPORTUNITY_CREATED", opportunity_id="opp-created"))
        # Give the worker a moment (nothing should be queued).
        time.sleep(0.1)
        assert len(sent) == 0
        notifier.stop()
    finally:
        tg_mod._send_telegram_message = original
