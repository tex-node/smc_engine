"""Outbound Telegram notification channel for SMC opportunity events.

Credentials are loaded from a local file at startup; if the file is absent or
malformed the notifier stays disabled and the scanner continues normally.

Security guarantees:
- Credentials are never written to logs, error messages, or API responses.
- The module is outbound-only: no bot polling, no commands, no trade execution.
- The queue is bounded; on overflow the newest notification is dropped
  (deterministically, and counted in diagnostics).
- The background worker is a daemon thread and does not block shutdown.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

_log = logging.getLogger(__name__)

# Event kinds that send notifications by default.
_DEFAULT_ENABLED_KINDS: frozenset[str] = frozenset({
    "READY",
    "ENTRY_TRIGGERED",
    "CONVERTED_TO_SETUP",
    "EXECUTION_READY",
    "OPPORTUNITY_SUPERSEDED",
    "INVALIDATED",
})

_MAX_QUEUE_SIZE = 50
_MIN_SEND_INTERVAL_S = 1.0
_SEND_TIMEOUT_S = 10.0

# Unambiguous source marker appended to every message this Python pipeline
# sends. A different, independent sender (e.g. an MQL5 EA) may share the same
# Telegram bot and chat, so the message text must identify its origin. This is
# purely informational; it changes no delivery behaviour.
_SOURCE_TAG = "source: smc_engine (python pipeline)"


def _event_ref(opp_id: str, kind: str, event_id: str) -> str:
    """Short, stable reference that uniquely identifies a logical event.

    Mirrors the notifier's deduplication identity ``(opportunity_id, kind,
    event_id)`` so the ref is identical for a true re-delivery and distinct for
    any different legitimate event — including two kinds of the same
    opportunity (e.g. CONVERTED_TO_SETUP vs EXECUTION_READY, which share an
    ``event_id``). It is a hash prefix — no private data is revealed.
    """
    if not opp_id and not kind and not event_id:
        return "-"
    return hashlib.sha256(f"{opp_id}|{kind}|{event_id}".encode("utf-8")).hexdigest()[:10]


def _notification_id(opp_id: str, kind: str, event_id: str) -> str:
    """Stable, unique id for one logical notification (durable outbox key)."""
    return hashlib.sha256(f"{opp_id}|{kind}|{event_id}".encode("utf-8")).hexdigest()


def _message_fingerprint(text: str) -> str:
    """Short fingerprint of the exact outbound text (for logs; secret-safe)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
_MAX_RETRY_AFTER_S = 60.0
_MAX_RETRIES = 2

# Durable outbox tunables.
_STALE_INFLIGHT_S = 300          # IN_FLIGHT older than this is recovered to PENDING
_MAX_ATTEMPTS = 5                # per-record attempt budget before FAILED_PERMANENT
_BASE_BACKOFF_S = 2.0
_MAX_BACKOFF_S = 300.0


def _load_credentials(path: str) -> tuple[str, str]:
    """Parse Token and ChatID from the credential file.

    Expected format (whitespace/quotes/semicolons are flexible):
        Token     = "value";
        ChatID    = "value";

    Returns (token, chat_id). Raises ValueError if either key is absent.
    Raises FileNotFoundError if the file does not exist.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    result: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lower()
        val = val.strip().rstrip(";").strip().strip('"').strip("'")
        if key == "token":
            result["token"] = val
        elif key == "chatid":
            result["chat_id"] = val
    token = result.get("token", "")
    chat_id = result.get("chat_id", "")
    if not token or not chat_id:
        raise ValueError("credential file missing Token or ChatID")
    return token, chat_id


def _format_message(kind: str, symbol: str, opp: Optional[object]) -> str:
    """Format a human-readable Telegram message for an opportunity event."""
    direction = getattr(opp, "direction", "UNKNOWN") if opp else "UNKNOWN"
    opp_type = getattr(opp, "opportunity_type", "") if opp else ""
    header = f"{symbol}\n{direction}" + (f" · {opp_type}" if opp_type else "")

    if kind == "READY":
        next_exp = getattr(opp, "next_expected", "") if opp else ""
        parts = [
            "SMC OPPORTUNITY READY",
            header,
            "State:\nREADY FOR MITIGATION",
        ]
        if next_exp:
            parts.append(f"Next:\n{next_exp}")
        parts.append("Gate:\nOBSERVATION ONLY")
        return "\n\n".join(parts)

    if kind == "ENTRY_TRIGGERED":
        return "\n\n".join([
            "SMC ENTRY TRIGGERED",
            header,
            "POI mitigation detected",
            "Entry condition:\nCONFIRMED",
            "Note:\nThis is NOT EXECUTION_READY.\nSetup has NOT been created.",
            "Gate:\nOBSERVATION ONLY",
        ])

    if kind in ("EXECUTION_READY", "CONVERTED_TO_SETUP"):
        risk = getattr(opp, "risk_status", "PENDING") if opp else "PENDING"
        return "\n\n".join([
            "SMC EXECUTION READY",
            header,
            "Trade setup created.",
            f"Risk:\n{risk or 'PENDING'}",
            "Gate:\nOBSERVATION ONLY",
        ])

    if kind == "OPPORTUNITY_SUPERSEDED":
        sup_by = getattr(opp, "superseded_by", "") if opp else ""
        sup_reason = getattr(opp, "supersession_reason", "") if opp else ""
        parts = ["SMC OPPORTUNITY SUPERSEDED", header]
        if sup_by:
            parts.append(f"Superseded by:\n{sup_by}")
        if sup_reason:
            parts.append(f"Reason:\n{sup_reason}")
        return "\n\n".join(parts)

    if kind == "INVALIDATED":
        detail = (
            getattr(opp, "blocker", "") or getattr(opp, "reason", "")
            if opp else ""
        ) or "unknown"
        return "\n\n".join([
            "SMC OPPORTUNITY INVALIDATED",
            header,
            f"Reason:\n{detail}",
        ])

    if kind == "EXPIRED":
        return "\n\n".join([
            "SMC OPPORTUNITY EXPIRED",
            header,
            "TTL exceeded.",
        ])

    if kind == "OPPORTUNITY_CREATED":
        return "\n\n".join([
            "SMC OPPORTUNITY CREATED",
            header,
            "Gate:\nOBSERVATION ONLY",
        ])

    return "\n\n".join([f"SMC {kind}", header])


def _is_test_runner_active() -> bool:
    """True when a pytest session is running (test-isolation guard).

    Detects pytest either via its per-test environment marker or its presence in
    ``sys.modules`` (covers collection/import-time execution).
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def _parse_send_response(body: bytes) -> dict:
    """Interpret the raw body of a Telegram sendMessage 2xx response.

    A 2xx response means Telegram ACCEPTED the message. The success body is
    normally JSON; an unparseable body must NOT be treated as a failure, since
    retrying would deliver the same message twice, so it is reported as
    delivered (``{"ok": True, "body_unparsed": True}``).

    Parsing is deliberately separated from the transport so the accept/retry
    decision can be exercised without any HTTP (and without weakening the
    test-runner isolation guard in ``_send_telegram_message``).
    """
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {"ok": True, "body_unparsed": True}


def _send_telegram_message(token: str, chat_id: str, text: str,
                           timeout: float = _SEND_TIMEOUT_S) -> dict:
    """Low-level Telegram sendMessage API call.

    The token appears only in the URL, never in logs or error messages.
    Raises urllib.error.HTTPError on non-2xx responses so the caller can
    extract retry_after from a 429 body.

    Defense-in-depth: this is the ONLY place that touches the network, so it
    refuses to send while a test runner is active. A mis-isolated test must
    never be able to deliver a real message; tests observe sends by stubbing the
    transport (monkeypatching this function), not by reaching the network.
    """
    if _is_test_runner_active():
        raise RuntimeError(
            "Telegram send refused: a test runner is active (isolation guard); "
            "stub _send_telegram_message instead of performing a real send")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return _parse_send_response(body)


class TelegramNotifier:
    """Outbound-only Telegram notification channel.

    Instantiate once in EngineHub; call notify() from the scan thread.
    A daemon background worker thread handles actual HTTP delivery.
    """

    def __init__(
        self,
        cred_path: str,
        enabled_kinds: Optional[frozenset] = None,
        min_send_interval: float = _MIN_SEND_INTERVAL_S,
        quarantine_check: Optional[Callable[[str], bool]] = None,
        ledger: Optional[object] = None,
    ):
        self._enabled = False
        self._configured = False
        self._token = ""
        self._chat_id = ""
        self._enabled_kinds: frozenset[str] = (
            enabled_kinds if enabled_kinds is not None else _DEFAULT_ENABLED_KINDS
        )
        self._min_send_interval = min_send_interval
        # Authoritative persistent quarantine predicate (opportunity_id -> bool).
        # Consulted immediately before every outbound send; see _is_quarantined.
        self._quarantine_check = quarantine_check
        # Optional durable outbox (durable intent + at-least-once attempts). When
        # present the in-memory queue is bypassed and the outbox is the queue.
        self._ledger = ledger
        self._wake = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._stop = threading.Event()
        self._last_send_time = 0.0
        self._lock = threading.Lock()
        self._diag: dict = {
            "telegram_enabled": False,
            "telegram_configured": False,
            "telegram_last_success": None,
            "telegram_last_failure": None,
            "telegram_send_count": 0,
            "telegram_failure_count": 0,
            "telegram_deduplicated_count": 0,
            "telegram_uncertain_count": 0,
            "telegram_dropped_count": 0,
            "telegram_quarantined_count": 0,
        }
        # Per-symbol scan->delivery counters so a symbol with no eligible event
        # can be distinguished from a symbol whose event never reached delivery.
        self._by_symbol: dict[str, dict] = {}
        # In-process deduplication guard keyed by the STABLE logical-event
        # identity (opportunity_id, kind, event_id). Cross-restart deduplication
        # is guaranteed by record_event_once() at the DB layer — result["events"]
        # only ever contains fresh events with a stable identity.
        self._sent_keys: set[tuple[str, str, str]] = set()

        if cred_path:
            try:
                token, chat_id = _load_credentials(cred_path)
                self._token = token
                self._chat_id = chat_id
                self._enabled = True
                self._configured = True
                self._diag["telegram_enabled"] = True
                self._diag["telegram_configured"] = True
            except FileNotFoundError:
                pass  # file absent; Telegram disabled, scanner continues normally
            except Exception:
                _log.debug("Telegram credentials invalid; notifications disabled")

        if self._enabled:
            self._worker_thread = threading.Thread(
                target=self._worker, daemon=True, name="telegram-notifier",
            )
            self._worker_thread.start()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def notify(self, event: dict, opp: Optional[object] = None) -> None:
        """Enqueue a notification (best-effort, non-blocking).

        Returns immediately. If the notifier is disabled, the kind is not
        in enabled_kinds, or the queue is full, the call is a no-op.

        Deduplication uses the STABLE logical-event identity
        (opportunity_id, kind, event_id) so that distinct symbols and distinct
        lifecycle events never collide, while a repeated scan of the same
        persisted event is suppressed.
        """
        if not self._enabled:
            return
        kind = str(event.get("kind", ""))
        if kind not in self._enabled_kinds:
            return
        symbol = str(event.get("symbol", "?"))
        opp_id = str(event.get("opportunity_id", ""))
        event_id = str(event.get("event_id", ""))
        key = (opp_id, kind, event_id)
        with self._lock:
            if key in self._sent_keys:
                self._diag["telegram_deduplicated_count"] += 1
                self._bump(symbol, "deduplicated")
                return
            self._sent_keys.add(key)
        ref = _event_ref(opp_id, kind, event_id)
        text = (_format_message(kind, symbol, opp)
                + f"\n\n\u2014 {_SOURCE_TAG} \u00b7 ref {ref}")
        item = {"symbol": symbol, "kind": kind, "event_id": event_id,
                "opp_id": opp_id, "ref": ref, "text": text}
        if self._ledger is not None:
            # Durable mode: persist intent BEFORE dispatch. Fail closed — never
            # send without a durable record. The outbox is the queue.
            try:
                self._ledger.record_intent(_notification_id(opp_id, kind, event_id),
                                           opp_id, kind, event_id, symbol, text)
                with self._lock:
                    self._bump(symbol, "queued")
                self._wake.set()
            except Exception:
                _log.warning(
                    "notification ledger unavailable; not enqueuing %s %s", symbol, kind)
            return
        try:
            self._queue.put_nowait(item)
            with self._lock:
                self._bump(symbol, "queued")
        except queue.Full:
            with self._lock:
                self._diag["telegram_dropped_count"] += 1
                self._bump(symbol, "dropped")
            _log.debug(
                "Telegram queue full; notification dropped for %s %s", symbol, kind,
            )

    def _bump(self, symbol: str, field: str) -> None:
        """Increment a per-symbol pipeline counter (caller holds self._lock)."""
        slot = self._by_symbol.setdefault(
            symbol, {"queued": 0, "delivered": 0, "failed": 0,
                     "uncertain": 0, "deduplicated": 0, "dropped": 0})
        slot[field] = slot.get(field, 0) + 1

    def diagnostics(self) -> dict:
        """Return a diagnostics snapshot. Never exposes credentials."""
        with self._lock:
            d = dict(self._diag)
            d["telegram_by_symbol"] = {k: dict(v) for k, v in self._by_symbol.items()}
            return d

    def stop(self) -> None:
        """Signal the background worker to stop."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        if self._ledger is not None:
            self._worker_durable()
            return
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._send_with_retry(item)
            finally:
                self._queue.task_done()

    def _worker_durable(self) -> None:
        """Drain the durable outbox: recover stale claims, then claim + dispatch."""
        try:
            self._ledger.recover_stale(_STALE_INFLIGHT_S)
        except Exception:
            _log.warning("outbox stale-claim recovery failed")
        while not self._stop.is_set():
            try:
                rec = self._ledger.claim_next()
            except Exception:
                _log.warning("outbox claim failed")
                rec = None
            if rec is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                self._dispatch_record(rec)
            except Exception:
                _log.warning("outbox dispatch failed for %s", rec.get("notification_id"))

    def _backoff_seconds(self, attempts: int) -> float:
        base = min(_BASE_BACKOFF_S * (2 ** max(0, attempts - 1)), _MAX_BACKOFF_S)
        return base * (0.5 + random.random() * 0.5)          # bounded jitter

    def _dispatch_record(self, rec: dict) -> None:
        nid = rec["notification_id"]
        opp_id = rec.get("opportunity_id", "")
        symbol = rec.get("symbol", "?")
        # Final quarantine check immediately before dispatch (fail-closed).
        if self._is_quarantined(opp_id):
            self._ledger.mark_suppressed(nid, "quarantined")
            with self._lock:
                self._diag["telegram_quarantined_count"] += 1
                self._bump(symbol, "quarantined")
            self._log_send(rec, "suppressed(quarantined)", 0)
            return
        status, detail, mid, retry_after = self._send_once(rec)
        if status == "delivered":
            self._ledger.mark_delivered(nid, mid)
            with self._lock:
                self._diag["telegram_send_count"] += 1
                self._diag["telegram_last_success"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._bump(symbol, "delivered")
            self._log_send(rec, "delivered", int(rec.get("attempts", 0)))
            return
        attempts = int(rec.get("attempts", 1))
        if status == "failed" or attempts >= _MAX_ATTEMPTS:
            self._ledger.mark_failed(nid, detail)
            self._record_failure(symbol)
            self._log_send(rec, f"failed({detail})", attempts)
            return
        delay = retry_after if retry_after else self._backoff_seconds(attempts)
        nxt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        self._ledger.mark_retry(nid, detail, nxt)
        if "ambiguous" in detail:
            with self._lock:
                self._diag["telegram_uncertain_count"] += 1
            self._log_send(rec, f"uncertain({detail})", attempts)
        else:
            self._log_send(rec, f"retry({detail})", attempts)

    def _send_once(self, item: dict):
        """One HTTP attempt (rate-limited). -> (status, detail, msg_id, retry_after).

        A non-2xx or ambiguous outcome is NEVER reported as delivered.
        """
        text = item.get("text", "")
        now = time.monotonic()
        elapsed = now - self._last_send_time
        if elapsed < self._min_send_interval:
            time.sleep(self._min_send_interval - elapsed)
        self._log_send(item, "attempt", int(item.get("attempts", 0)))
        try:
            body = _send_telegram_message(self._token, self._chat_id, text)
            self._last_send_time = time.monotonic()
            mid = None
            if isinstance(body, dict):
                mid = str((body.get("result") or {}).get("message_id", "")) or None
            return ("delivered", "ok", mid, None)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = _BASE_BACKOFF_S
                try:
                    ra = json.loads(exc.read()).get("parameters", {}).get("retry_after")
                    if ra is not None:
                        retry_after = min(float(ra), _MAX_RETRY_AFTER_S)
                except Exception:
                    pass
                return ("retry", f"http 429 retry_after={retry_after:g}", None, retry_after)
            return ("failed", f"http {exc.code}", None, None)
        except (TimeoutError, socket.timeout):
            return ("retry", "ambiguous(timeout)", None, None)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
                return ("retry", "pre-delivery", None, None)
            return ("retry", "ambiguous(urlerror)", None, None)
        except Exception:
            return ("retry", "ambiguous(error)", None, None)

    def _log_send(self, item: dict, outcome: str, attempt: int = 0) -> None:
        """Secret-safe structured log line for every send attempt/outcome."""
        _log.info(
            "telegram_send pid=%s ref=%s fp=%s symbol=%s kind=%s event_id=%s "
            "outcome=%s attempt=%s",
            os.getpid(), item.get("ref", "-"),
            _message_fingerprint(item.get("text", "")),
            item.get("symbol", "?"), item.get("kind", ""),
            item.get("event_id", ""), outcome, attempt,
        )

    def _is_quarantined(self, opp_id: str) -> bool:
        """Authoritative, persistent quarantine check at the send boundary.

        Fail-closed: returns True (do not deliver) when the state cannot be
        established. A storage error must never look like 'not quarantined'.
        """
        if not opp_id or self._quarantine_check is None:
            return False
        try:
            return bool(self._quarantine_check(opp_id))
        except Exception:
            _log.warning(
                "telegram quarantine check failed for %s; suppressing send", opp_id)
            return True

    def _send_with_retry(self, item: dict) -> None:
        """Send one message with rate limiting and bounded exponential backoff.

        Failure policy (documented):
          * HTTP 429                -> retried, honouring ``retry_after``.
          * URLError DNS / refused  -> DEFINITIVE pre-delivery failure; retried.
          * Timeout / reset / other -> AMBIGUOUS: the request may already have
            reached Telegram, so it is recorded as ``uncertain`` and NOT retried
            (at-most-once). Telegram's Bot API offers no idempotency key, so
            exactly-once cannot be guaranteed across ambiguous outcomes.
        """
        text = item.get("text", "")
        symbol = item.get("symbol", "?")
        delay = 2.0
        for attempt in range(_MAX_RETRIES + 1):
            # Rate limiting: enforce minimum interval between sends.
            now = time.monotonic()
            elapsed = now - self._last_send_time
            if elapsed < self._min_send_interval:
                time.sleep(self._min_send_interval - elapsed)

            # FINAL quarantine eligibility check immediately before the send,
            # using the authoritative persistent state (a queued event is still
            # suppressed if the opportunity was quarantined after enqueue).
            if self._is_quarantined(item.get("opp_id", "")):
                with self._lock:
                    self._diag["telegram_quarantined_count"] += 1
                    self._bump(symbol, "quarantined")
                self._log_send(item, "suppressed(quarantined)", attempt)
                return
            self._log_send(item, "attempt", attempt)
            try:
                _send_telegram_message(self._token, self._chat_id, text)
                self._last_send_time = time.monotonic()
                with self._lock:
                    self._diag["telegram_send_count"] += 1
                    self._diag["telegram_last_success"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    self._bump(symbol, "delivered")
                self._log_send(item, "delivered", attempt)
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_after = delay
                    try:
                        body = json.loads(exc.read())
                        retry_after = float(
                            body.get("parameters", {}).get("retry_after", delay)
                        )
                        retry_after = min(retry_after, _MAX_RETRY_AFTER_S)
                    except Exception:
                        pass
                    if attempt < _MAX_RETRIES:
                        self._log_send(item, "retrying(429)", attempt)
                        time.sleep(retry_after)
                        delay = min(delay * 2, _MAX_RETRY_AFTER_S)
                        continue
                # Non-429 HTTP error or retries exhausted.
                self._record_failure(symbol)
                self._log_send(item, f"failed(http {exc.code})", attempt)
                return
            except (TimeoutError, socket.timeout):
                # Ambiguous: do not retry (would risk a duplicate).
                self._record_uncertain(symbol)
                self._log_send(item, "uncertain(timeout)", attempt)
                return
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                definitive = isinstance(reason, (socket.gaierror, ConnectionRefusedError))
                if definitive and attempt < _MAX_RETRIES:
                    # Pre-delivery failure: nothing was sent; safe to retry.
                    self._log_send(item, "retrying(pre-delivery)", attempt)
                    time.sleep(delay)
                    delay = min(delay * 2, _MAX_RETRY_AFTER_S)
                    continue
                # Ambiguous (e.g. connection reset after the request may have
                # been sent) or retries exhausted: do NOT retry (at-most-once).
                self._record_uncertain(symbol)
                self._log_send(item, "uncertain(urlerror)", attempt)
                return
            except Exception:
                # Unknown/ambiguous: do not retry (would risk a duplicate).
                self._record_uncertain(symbol)
                self._log_send(item, "uncertain(error)", attempt)
                return

    def _record_failure(self, symbol: str = "?") -> None:
        with self._lock:
            self._diag["telegram_failure_count"] += 1
            self._diag["telegram_last_failure"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            self._bump(symbol, "failed")

    def _record_uncertain(self, symbol: str = "?") -> None:
        with self._lock:
            self._diag["telegram_failure_count"] += 1
            self._diag["telegram_uncertain_count"] += 1
            self._diag["telegram_last_failure"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            self._bump(symbol, "uncertain")
