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

import json
import logging
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

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
_MAX_RETRY_AFTER_S = 60.0
_MAX_RETRIES = 2


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


def _send_telegram_message(token: str, chat_id: str, text: str,
                           timeout: float = _SEND_TIMEOUT_S) -> dict:
    """Low-level Telegram sendMessage API call.

    The token appears only in the URL, never in logs or error messages.
    Raises urllib.error.HTTPError on non-2xx responses so the caller can
    extract retry_after from a 429 body.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        # A 2xx response means Telegram ACCEPTED the message. An unparseable
        # body must NOT be treated as a failure: retrying here would deliver
        # the same message twice.
        return {"ok": True, "body_unparsed": True}


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
    ):
        self._enabled = False
        self._configured = False
        self._token = ""
        self._chat_id = ""
        self._enabled_kinds: frozenset[str] = (
            enabled_kinds if enabled_kinds is not None else _DEFAULT_ENABLED_KINDS
        )
        self._min_send_interval = min_send_interval
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
        text = _format_message(kind, symbol, opp)
        item = {"symbol": symbol, "kind": kind, "event_id": event_id, "text": text}
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
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._send_with_retry(item)
            finally:
                self._queue.task_done()

    def _send_with_retry(self, item: dict) -> None:
        """Send one message with rate limiting and bounded exponential backoff.

        Failure policy (documented):
          * HTTP 429            -> retried, honouring ``retry_after``.
          * URLError (DNS/refused) -> definitive pre-delivery failure; retried.
          * Timeout / other      -> AMBIGUOUS: the request may already have
            reached Telegram, so it is recorded as ``uncertain`` and NOT retried
            (preferring at-most-once over a duplicate delivery). Telegram's Bot
            API offers no idempotency key, so exactly-once cannot be guaranteed
            across ambiguous network outcomes.
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

            try:
                _send_telegram_message(self._token, self._chat_id, text)
                self._last_send_time = time.monotonic()
                with self._lock:
                    self._diag["telegram_send_count"] += 1
                    self._diag["telegram_last_success"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    self._bump(symbol, "delivered")
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
                        time.sleep(retry_after)
                        delay = min(delay * 2, _MAX_RETRY_AFTER_S)
                        continue
                # Non-429 HTTP error or retries exhausted.
                self._record_failure(symbol)
                return
            except (TimeoutError, socket.timeout):
                # Ambiguous: do not retry (would risk a duplicate).
                self._record_uncertain(symbol)
                return
            except urllib.error.URLError:
                # Definitive pre-delivery failure (DNS / connection refused).
                if attempt < _MAX_RETRIES:
                    time.sleep(delay)
                    delay = min(delay * 2, _MAX_RETRY_AFTER_S)
                    continue
                self._record_failure(symbol)
                return
            except Exception:
                # Unknown/ambiguous: do not retry (would risk a duplicate).
                self._record_uncertain(symbol)
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
