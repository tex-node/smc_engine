"""Persistent causal setup-event history — audit/observability only.

This module records genuine engine-causal setup discoveries into a durable
SQLite audit trail. It NEVER calls broker primitives (no submit, preflight or
trade-action primitives exist in this file) and NEVER initiates execution.
It observes:

  - first detection of a canonical causal setup (immutable snapshot)
  - subsequent observations (last_seen + observation count only)
  - disappearance / expiry ("NO LONGER PRESENT" — distinct from lifecycle
    breaches, which are recorded with their real state name)
  - existing lifecycle transitions (observed, never invented)
  - manual review actions
  - paper execution outcomes (recorded after the fact by the hub; this
    module never triggers them)

Canonical identity comes from the engine itself:
`build_trade_setup` emits ids of the form  SETUP-{SYMBOL}-{csd_index}-{OB_ID}
where OB_ID starts with "OB-". Anything not matching that production shape
(incl. Gate A synthetic labels and generic test ids) is rejected from
history unless explicitly persisted via a test-only fixture path.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from typing import Any, Optional

import pandas as pd

# engine's own id shape: SETUP-<SYMBOL>-<int>OB-...  (see build_trade_setup)
CANONICAL_SETUP_ID = re.compile(r"^SETUP-[A-Z][A-Z0-9]*-\d+-OB-[A-Z0-9.\-]+$")

# audit-side statuses. NOT trading lifecycle states (those stay in the engine).
STATUS_DETECTED = "DETECTED"
STATUS_ACTIVE = "ACTIVE"
STATUS_EXPIRED = "EXPIRED"
STATUS_CLOSED = "CLOSED"
HISTORY_STATUSES = (STATUS_DETECTED, STATUS_ACTIVE, STATUS_EXPIRED, STATUS_CLOSED)

# engine states that are terminal breaches / closes, mapped to audit semantics
_BREACH_STATES = {"PROTECTED_LEVEL_BREACHED", "POI_INVALIDATED", "OB_INVALIDATED",
                  "CSD_EXPIRED", "ENTRY_NO_LONGER_VALID", "RISK_REJECTED",
                  "BROKER_REJECTED"}

_REQUIRED_NUMERIC = ("entry", "stop_loss", "take_profit", "risk_percent")
_REQUIRED_TEXT = ("setup_id", "symbol", "direction", "as_of")


def _utcnow() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def _norm(symbol: str) -> str:
    return "".join(ch for ch in str(symbol).upper() if ch.isalnum())


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def canonical_reason(payload: dict) -> Optional[str]:
    """Why a payload is not a genuine causal setup event, or None if valid."""
    sid = payload.get("setup_id")
    if not isinstance(sid, str) or not sid:
        return "missing setup_id"
    if not CANONICAL_SETUP_ID.match(sid):
        return "non-canonical setup id"
    if not isinstance(payload.get("symbol"), str) or not payload.get("symbol"):
        return "missing symbol"
    if not isinstance(payload.get("direction"), str) or payload["direction"] not in ("BULLISH", "BEARISH"):
        return "missing/invalid direction"
    for f in _REQUIRED_NUMERIC:
        if not _is_number(payload.get(f)):
            return f"missing/non-numeric {f}"
    if not isinstance(payload.get("as_of"), str) or not payload.get("as_of"):
        return "missing as_of"
    ev = payload.get("evidence")
    if not isinstance(ev, dict) or not ev:
        return "missing evidence"
    return None


def timeframe_from_id(setup_id: str) -> str:
    """Execution timeframe parsed from the engine's OB identity, never invented."""
    m = CANONICAL_SETUP_ID.match(setup_id or "")
    if not m:
        return "MTF"
    ob_part = setup_id.split("-OB-", 1)[1]
    tf = ob_part.split("-", 1)[0]
    return tf if tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1") else "M15"


class SetupEventHistory:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS setup_events(
          row_id INTEGER PRIMARY KEY AUTOINCREMENT,
          setup_id TEXT NOT NULL UNIQUE,
          symbol TEXT NOT NULL,
          symbol_norm TEXT NOT NULL,
          timeframe TEXT,
          direction TEXT,
          entry REAL, stop_loss REAL, take_profit REAL, risk_percent REAL, rr REAL,
          as_of TEXT,
          first_seen_at TEXT, last_seen_at TEXT,
          status TEXT, close_reason TEXT, closed_at TEXT,
          evidence_json TEXT,
          reviewed_at TEXT, review_count INTEGER DEFAULT 0,
          paper_execution_requested_at TEXT, paper_execution_outcome TEXT,
          observations INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS setup_event_timeline(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          setup_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_symbol_status
          ON setup_events(symbol_norm, status);
        CREATE INDEX IF NOT EXISTS idx_events_first_seen
          ON setup_events(first_seen_at);
        """)
        self._conn.commit()
        self._lock = threading.Lock()

    # ---------- write ----------
    def observe(self, payload: dict) -> str:
        """Idempotent record. One row per canonical setup_id."""
        reason = canonical_reason(payload)
        if reason is not None:
            return f"rejected: {reason}"
        now = _utcnow()
        sid = payload["setup_id"]
        tf = payload.get("timeframe") or timeframe_from_id(sid)
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO setup_events
                   (setup_id, symbol, symbol_norm, timeframe, direction,
                    entry, stop_loss, take_profit, risk_percent, rr,
                    as_of, first_seen_at, last_seen_at, status, evidence_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, payload["symbol"], _norm(payload["symbol"]), tf,
                 payload["direction"], float(payload["entry"]),
                 float(payload["stop_loss"]), float(payload["take_profit"]),
                 float(payload["risk_percent"]), payload.get("rr"),
                 payload["as_of"], now, now, STATUS_DETECTED,
                 json.dumps(payload["evidence"], sort_keys=True)))
            if cur.rowcount:
                self._conn.execute(
                    "INSERT INTO setup_event_timeline(setup_id,at,kind,detail) VALUES(?,?,?,?)",
                    (sid, now, "DETECTED",
                     f"first observed ({payload['symbol']} {tf} {payload['direction']})"))
                self._conn.commit()
                return "inserted"
            # existing: update observation metadata ONLY.
            # first_seen_at, as_of and evidence snapshot are never rewritten (§6/§19).
            row = self._conn.execute(
                "SELECT status FROM setup_events WHERE setup_id=?", (sid,)).fetchone()
            self._conn.execute(
                """UPDATE setup_events
                   SET last_seen_at=?, observations=observations+1,
                       status=CASE WHEN status IN(?,?) THEN ? ELSE status END,
                       close_reason=CASE WHEN status=? THEN NULL ELSE close_reason END,
                       closed_at=CASE WHEN status=? THEN NULL ELSE closed_at END
                   WHERE setup_id=?""",
                (now, STATUS_DETECTED, STATUS_EXPIRED, STATUS_ACTIVE,
                 STATUS_EXPIRED, STATUS_EXPIRED, sid))
            if row and row[0] == STATUS_EXPIRED:
                self._conn.execute(
                    "INSERT INTO setup_event_timeline(setup_id,at,kind,detail) VALUES(?,?,?,?)",
                    (sid, now, "REVIVED", "reappeared in production pipeline"))
            self._conn.commit()
            return "updated"

    def expire_absent(self, symbol: str, current_ids: set) -> int:
        """Setups no longer produced by the production readiness path.
        Never deletes; appends EXPIRED audit state. current_ids must be the
        FULL fresh candidate id set for this symbol."""
        sym_norm = _norm(symbol)
        now = _utcnow()
        with self._lock:
            rows = self._conn.execute(
                """SELECT setup_id FROM setup_events
                   WHERE symbol_norm=? AND status IN(?,?)""",
                (sym_norm, STATUS_DETECTED, STATUS_ACTIVE)).fetchall()
            expired = 0
            for (sid,) in rows:
                if sid in current_ids:
                    continue
                self._conn.execute(
                    """UPDATE setup_events
                       SET status=?, close_reason=?, closed_at=?
                       WHERE setup_id=? AND status IN(?,?)""",
                    (STATUS_EXPIRED, "NO LONGER PRESENT", now, sid,
                     STATUS_DETECTED, STATUS_ACTIVE))
                self._conn.execute(
                    "INSERT INTO setup_event_timeline(setup_id,at,kind,detail) VALUES(?,?,?,?)",
                    (sid, now, "EXPIRED", "no longer present in production pipeline"))
                expired += 1
            self._conn.commit()
            return expired

    def record_lifecycle(self, setup_id: str, from_state: str, to_state: str,
                         reason: str) -> None:
        """Observe an existing lifecycle transition (never invents one)."""
        now = _utcnow()
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM setup_events WHERE setup_id=?", (setup_id,)).fetchone()
            if not exists:
                return
            self._conn.execute(
                "INSERT INTO setup_event_timeline(setup_id,at,kind,detail) VALUES(?,?,?,?)",
                (setup_id, now, "STATE", f"{from_state} -> {to_state}: {reason}"))
            if to_state == "CLOSED":
                self._conn.execute(
                    """UPDATE setup_events SET status=?, close_reason=?, closed_at=?
                       WHERE setup_id=? AND status NOT IN(?,?)""",
                    (STATUS_CLOSED, reason or "CLOSED", now, setup_id,
                     STATUS_CLOSED, STATUS_EXPIRED))
            elif to_state in _BREACH_STATES:
                self._conn.execute(
                    """UPDATE setup_events SET status=?, close_reason=?, closed_at=?
                       WHERE setup_id=? AND status NOT IN(?,?)""",
                    (STATUS_EXPIRED, to_state, now, setup_id,
                     STATUS_CLOSED, STATUS_EXPIRED))
            self._conn.commit()

    def mark_reviewed(self, setup_id: str) -> bool:
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE setup_events
                   SET reviewed_at=COALESCE(reviewed_at, ?), review_count=review_count+1
                   WHERE setup_id=?""", (now, setup_id))
            if cur.rowcount:
                self._conn.execute(
                    "INSERT INTO setup_event_timeline(setup_id,at,kind,detail) VALUES(?,?,?,?)",
                    (setup_id, now, "REVIEWED", "manual review (anonymous session)"))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_paper_execution(self, setup_id: str, outcome: str,
                             requested: bool = True) -> None:
        """Record AFTER-the-fact paper execution observations made by the hub.
        This method itself never executes anything."""
        now = _utcnow()
        with self._lock:
            if not self._conn.execute(
                    "SELECT 1 FROM setup_events WHERE setup_id=?", (setup_id,)).fetchone():
                return
            if requested:
                self._conn.execute(
                    """UPDATE setup_events
                       SET paper_execution_requested_at=COALESCE(
                           paper_execution_requested_at, ?), paper_execution_outcome=?
                       WHERE setup_id=?""", (now, outcome, setup_id))
            else:
                self._conn.execute(
                    "UPDATE setup_events SET paper_execution_outcome=? WHERE setup_id=?",
                    (outcome, setup_id))
            self._conn.execute(
                "INSERT INTO setup_event_timeline(setup_id,at,kind,detail) VALUES(?,?,?,?)",
                (setup_id, now, "PAPER", outcome))
            self._conn.commit()

    # ---------- read ----------
    def query(self, symbol: Optional[str] = None, timeframe: Optional[str] = None,
              status: Optional[str] = None, setup_id: Optional[str] = None,
              limit: int = 50, before_id: Optional[int] = None) -> dict:
        limit = max(1, min(int(limit), 200))
        sql = ("SELECT row_id, setup_id, symbol, timeframe, direction, entry, stop_loss, "
               "take_profit, risk_percent, rr, as_of, first_seen_at, last_seen_at, status, "
               "close_reason, closed_at, evidence_json, reviewed_at, review_count, "
               "paper_execution_requested_at, paper_execution_outcome, observations "
               "FROM setup_events WHERE 1=1")
        args = []
        if symbol:
            sql += " AND symbol_norm=?"
            args.append(_norm(symbol))
        if timeframe:
            sql += " AND timeframe=?"
            args.append(timeframe)
        if status:
            sql += " AND status=?"
            args.append(status)
        if setup_id:
            sql += " AND setup_id=?"
            args.append(setup_id)
        if before_id is not None:
            sql += " AND row_id < ?"
            args.append(int(before_id))
        sql += " ORDER BY row_id DESC LIMIT ?"
        args.append(limit + 1)
        rows = self._conn.execute(sql, args).fetchall()
        cols = ["row_id", "setup_id", "symbol", "timeframe", "direction", "entry",
                "stop_loss", "take_profit", "risk_percent", "rr", "as_of",
                "first_seen_at", "last_seen_at", "status", "close_reason", "closed_at",
                "evidence", "reviewed_at", "review_count",
                "paper_execution_requested_at", "paper_execution_outcome", "observations"]
        events = []
        for r in rows[:limit]:
            d = dict(zip(cols, r))
            try:
                d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else None
            except (TypeError, ValueError):
                d["evidence"] = d["evidence"]  # preserve raw if unparseable
            events.append(d)
        next_cursor = rows[limit][0] if len(rows) > limit else None
        return {"events": events, "next_cursor": next_cursor}

    def timeline(self, setup_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT at, kind, detail FROM setup_event_timeline WHERE setup_id=? "
            "ORDER BY id ASC", (setup_id,)).fetchall()
        return [{"at": a, "kind": k, "detail": d} for a, k, d in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM setup_events").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
