"""Durable notification outbox for the outbound Telegram channel.

Records notification INTENT before dispatch, tracks attempts and outcomes, and
survives process restart. It provides durable creation + at-least-once delivery
ATTEMPTS with confirmed Telegram acceptance recorded — it does NOT provide
exactly-once external delivery (a timeout after Telegram accepts a message is
ambiguous and is never recorded as DELIVERED).

State machine (validated transitions):
    PENDING   -> IN_FLIGHT -> {DELIVERED, RETRY_WAIT, FAILED_PERMANENT, SUPPRESSED_QUARANTINE}
    RETRY_WAIT -> IN_FLIGHT (when next_attempt_at <= now)
    IN_FLIGHT -> PENDING (stale-claim recovery after a process crash)

Additive table only; no destructive change. Shares the GUI DB writer lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from .web.dbwrite import serialized_write
from .web.dbwrite import serialized_read as _serialized_read

PENDING = "PENDING"
IN_FLIGHT = "IN_FLIGHT"
DELIVERED = "DELIVERED"
RETRY_WAIT = "RETRY_WAIT"
FAILED_PERMANENT = "FAILED_PERMANENT"
SUPPRESSED_QUARANTINE = "SUPPRESSED_QUARANTINE"

TERMINAL = {DELIVERED, FAILED_PERMANENT, SUPPRESSED_QUARANTINE}
_CLAIMABLE = (PENDING, RETRY_WAIT)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NotificationLedger:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS notification_outbox(
          notification_id TEXT PRIMARY KEY,
          opportunity_id TEXT,
          kind TEXT NOT NULL,
          event_id TEXT,
          symbol TEXT,
          text TEXT NOT NULL,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT,
          last_attempt_at TEXT,
          last_error TEXT,
          telegram_message_id TEXT,
          delivered_at TEXT,
          suppression_reason TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(opportunity_id, kind, event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_status
          ON notification_outbox(status, next_attempt_at);
        """)
        self._conn.commit()
        self._lock = threading.RLock()

    # ---------- creation (idempotent) ----------
    @serialized_write
    def record_intent(self, notification_id: str, opportunity_id: str, kind: str,
                      event_id: str, symbol: str, text: str) -> bool:
        """Durably record the intent BEFORE dispatch. True if newly created."""
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO notification_outbox
                   (notification_id, opportunity_id, kind, event_id, symbol, text,
                    status, attempts, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,0,?,?)""",
                (notification_id, opportunity_id, kind, event_id, symbol, text,
                 PENDING, now, now))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- claims ----------
    @serialized_write
    def claim(self, notification_id: str) -> bool:
        """Atomically claim one record (PENDING/RETRY_WAIT -> IN_FLIGHT)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE notification_outbox SET status=?, last_attempt_at=?, updated_at=? "
                "WHERE notification_id=? AND status IN (?,?)",
                (IN_FLIGHT, _now_iso(), _now_iso(), notification_id, PENDING, RETRY_WAIT))
            self._conn.commit()
            return cur.rowcount > 0

    @serialized_write
    def claim_next(self, now: Optional[str] = None) -> Optional[dict]:
        """Claim the oldest due record; None if nothing is due."""
        now = now or _now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT notification_id FROM notification_outbox "
                "WHERE status IN (?,?) AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "ORDER BY created_at ASC LIMIT 1",
                (PENDING, RETRY_WAIT, now)).fetchone()
            if row is None:
                return None
            nid = row[0]
            cur = self._conn.execute(
                "UPDATE notification_outbox SET status=?, attempts=attempts+1, "
                "last_attempt_at=?, updated_at=? WHERE notification_id=? AND status IN (?,?)",
                (IN_FLIGHT, now, now, nid, PENDING, RETRY_WAIT))
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return self._row(nid)

    @serialized_write
    def recover_stale(self, stale_seconds: int = 300,
                      now: Optional[str] = None) -> int:
        """Return crashed IN_FLIGHT claims to PENDING (documented recovery)."""
        now_ts = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        cutoff = (now_ts - timedelta(seconds=stale_seconds)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE notification_outbox SET status=?, updated_at=? "
                "WHERE status=? AND last_attempt_at IS NOT NULL AND last_attempt_at<?",
                (PENDING, _now_iso(), IN_FLIGHT, cutoff))
            self._conn.commit()
            return cur.rowcount

    # ---------- outcomes ----------
    @serialized_write
    def mark_delivered(self, notification_id: str, telegram_message_id: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notification_outbox SET status=?, telegram_message_id=?, "
                "delivered_at=?, last_error=NULL, updated_at=? WHERE notification_id=?",
                (DELIVERED, telegram_message_id, _now_iso(), _now_iso(), notification_id))
            self._conn.commit()

    @serialized_write
    def mark_retry(self, notification_id: str, error: str, next_attempt_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notification_outbox SET status=?, last_error=?, "
                "next_attempt_at=?, updated_at=? WHERE notification_id=?",
                (RETRY_WAIT, error, next_attempt_at, _now_iso(), notification_id))
            self._conn.commit()

    @serialized_write
    def mark_failed(self, notification_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notification_outbox SET status=?, last_error=?, updated_at=? "
                "WHERE notification_id=?",
                (FAILED_PERMANENT, error, _now_iso(), notification_id))
            self._conn.commit()

    @serialized_write
    def mark_suppressed(self, notification_id: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notification_outbox SET status=?, suppression_reason=?, updated_at=? "
                "WHERE notification_id=?",
                (SUPPRESSED_QUARANTINE, reason, _now_iso(), notification_id))
            self._conn.commit()

    # ---------- reads ----------
    def _row(self, notification_id: str) -> Optional[dict]:
        r = self._conn.execute(
            "SELECT notification_id, opportunity_id, kind, event_id, symbol, text, "
            "status, attempts, next_attempt_at, last_attempt_at, last_error, "
            "telegram_message_id, delivered_at, suppression_reason, created_at, updated_at "
            "FROM notification_outbox WHERE notification_id=?", (notification_id,)).fetchone()
        if r is None:
            return None
        keys = ("notification_id", "opportunity_id", "kind", "event_id", "symbol", "text",
                "status", "attempts", "next_attempt_at", "last_attempt_at", "last_error",
                "telegram_message_id", "delivered_at", "suppression_reason", "created_at",
                "updated_at")
        return dict(zip(keys, r))

    @_serialized_read
    def get(self, notification_id: str) -> Optional[dict]:
        return self._row(notification_id)

    @_serialized_read
    def counts(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM notification_outbox GROUP BY status").fetchall()
        return {s: n for s, n in rows}

    @_serialized_read
    def diagnostics(self, limit: int = 50) -> list:
        rows = self._conn.execute(
            "SELECT notification_id, opportunity_id, kind, status, attempts, last_error, "
            "suppression_reason FROM notification_outbox ORDER BY updated_at DESC LIMIT ?",
            (int(limit),)).fetchall()
        keys = ("notification_id", "opportunity_id", "kind", "status", "attempts",
                "last_error", "suppression_reason")
        return [dict(zip(keys, r)) for r in rows]

    @_serialized_read
    def pending_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status IN (?,?)",
            (PENDING, RETRY_WAIT)).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
