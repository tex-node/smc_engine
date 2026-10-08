"""Opportunity persistence (GUI-owned SQLite, same database as setup history).

Additive migration only: CREATE TABLE IF NOT EXISTS, no destructive changes to
existing setup/history/lifecycle tables. Restart recovery = load_active().
Alert idempotency = UNIQUE(opportunity_id, transition, evidence_key).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from typing import Optional

import pandas as pd

from .models import Opportunity
from ..web.dbwrite import serialized_write
from ..web.dbwrite import serialized_read as _serialized_read


def _utcnow() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


class OpportunityRepository:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS opportunities(
          opportunity_id TEXT PRIMARY KEY,
          canonical_key TEXT NOT NULL UNIQUE,
          symbol TEXT NOT NULL,
          symbol_norm TEXT NOT NULL,
          direction TEXT NOT NULL,
          opportunity_type TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT, updated_at TEXT, expires_at TEXT,
          first_seen TEXT, last_seen TEXT,
          setup_id TEXT, risk_status TEXT,
          reason TEXT, blocker TEXT, next_expected TEXT,
          payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS opportunity_state_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opportunity_id TEXT NOT NULL,
          at TEXT NOT NULL,
          from_state TEXT, to_state TEXT NOT NULL,
          reason TEXT, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS opportunity_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opportunity_id TEXT NOT NULL,
          at TEXT NOT NULL,
          transition TEXT NOT NULL,
          evidence_key TEXT NOT NULL,
          kind TEXT NOT NULL,
          message TEXT,
          UNIQUE(opportunity_id, transition, evidence_key)
        );
        CREATE INDEX IF NOT EXISTS idx_opp_symbol_state
          ON opportunities(symbol_norm, state);
        CREATE INDEX IF NOT EXISTS idx_opp_updated
          ON opportunities(updated_at);
        CREATE INDEX IF NOT EXISTS idx_opp_hist
          ON opportunity_state_history(opportunity_id);
        """)
        self._conn.commit()
        self._lock = threading.RLock()

    # ---------- write ----------
    @serialized_write
    def upsert(self, opp: Opportunity) -> None:
        d = asdict(opp)
        with self._lock:
            self._conn.execute(
                """INSERT INTO opportunities
                   (opportunity_id, canonical_key, symbol, symbol_norm, direction,
                    opportunity_type, state, created_at, updated_at, expires_at,
                    first_seen, last_seen, setup_id, risk_status, reason, blocker,
                    next_expected, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(opportunity_id) DO UPDATE SET
                     state=excluded.state, updated_at=excluded.updated_at,
                     expires_at=excluded.expires_at, last_seen=excluded.last_seen,
                     setup_id=excluded.setup_id, risk_status=excluded.risk_status,
                     reason=excluded.reason, blocker=excluded.blocker,
                     next_expected=excluded.next_expected,
                     payload_json=excluded.payload_json""",
                (opp.opportunity_id, opp.canonical_key, opp.symbol,
                 "".join(c for c in opp.symbol.upper() if c.isalnum()),
                 opp.direction, opp.opportunity_type, opp.state,
                 str(opp.created_at), str(opp.updated_at),
                 str(opp.expires_at) if opp.expires_at else None,
                 str(opp.first_seen), str(opp.last_seen), opp.setup_id,
                 opp.risk_status, opp.reason, opp.blocker, opp.next_expected,
                 json.dumps(d, default=str, sort_keys=True)))
            self._conn.commit()

    @serialized_write
    def record_state_change(self, opportunity_id: str, from_state: str, to_state: str,
                            reason: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO opportunity_state_history
                   (opportunity_id, at, from_state, to_state, reason, detail)
                   VALUES(?,?,?,?,?,?)""",
                (opportunity_id, _utcnow(), from_state, to_state, reason, detail))
            self._conn.commit()

    @serialized_write
    def record_event_once(self, opportunity_id: str, transition: str,
                          evidence_key: str, kind: str, message: str) -> bool:
        """True when this (opportunity, transition, evidence) is newly recorded.

        The UNIQUE constraint makes repeated polling cycles idempotent: the
        same READY transition on the same evidence timestamp alerts once.
        """
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO opportunity_events
                   (opportunity_id, at, transition, evidence_key, kind, message)
                   VALUES(?,?,?,?,?,?)""",
                (opportunity_id, _utcnow(), transition, evidence_key, kind, message))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- read ----------
    def _row_to_opp(self, row) -> Optional[Opportunity]:
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        try:
            return Opportunity(**payload)
        except TypeError:
            return None

    @_serialized_read
    def get(self, opportunity_id: str) -> Optional[Opportunity]:
        r = self._conn.execute(
            "SELECT payload_json FROM opportunities WHERE opportunity_id=?",
            (opportunity_id,)).fetchone()
        return self._row_to_opp(r) if r else None

    @_serialized_read
    def get_by_key(self, canonical_key: str) -> Optional[Opportunity]:
        r = self._conn.execute(
            "SELECT payload_json FROM opportunities WHERE canonical_key=?",
            (canonical_key,)).fetchone()
        return self._row_to_opp(r) if r else None

    @_serialized_read
    def query(self, symbol: Optional[str] = None, state: Optional[str] = None,
              opportunity_type: Optional[str] = None, active_only: bool = False,
              limit: int = 200) -> list[Opportunity]:
        sql = "SELECT payload_json FROM opportunities WHERE 1=1"
        args: list = []
        if symbol:
            sql += " AND symbol_norm=?"
            args.append("".join(c for c in symbol.upper() if c.isalnum()))
        if state:
            sql += " AND state=?"
            args.append(state)
        if opportunity_type:
            sql += " AND opportunity_type=?"
            args.append(opportunity_type)
        if active_only:
            sql += (" AND state NOT IN "
                    "('INVALIDATED','EXPIRED','TERMINAL','ENTRY_TRIGGERED','SUPERSEDED')")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for (payload,) in self._conn.execute(sql, args).fetchall():
            try:
                out.append(Opportunity(**json.loads(payload)))
            except (TypeError, ValueError):
                continue
        return out

    @_serialized_read
    def history(self, opportunity_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT at, from_state, to_state, reason, detail FROM "
            "opportunity_state_history WHERE opportunity_id=? ORDER BY id ASC",
            (opportunity_id,)).fetchall()
        return [{"at": a, "from": f, "to": t, "reason": r, "detail": d}
                for a, f, t, r, d in rows]

    @_serialized_read
    def active_for_instrument(self, symbol_norm: str) -> list[Opportunity]:
        """All currently active opportunities for a canonical instrument (by norm_symbol)."""
        sql = ("SELECT payload_json FROM opportunities WHERE symbol_norm=? "
               "AND state NOT IN "
               "('INVALIDATED','EXPIRED','TERMINAL','ENTRY_TRIGGERED','SUPERSEDED') "
               "ORDER BY updated_at DESC LIMIT 200")
        out = []
        for (payload,) in self._conn.execute(sql, (symbol_norm,)).fetchall():
            try:
                out.append(Opportunity(**json.loads(payload)))
            except (TypeError, ValueError):
                continue
        return out

    @_serialized_read
    def state_counts(self) -> dict:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) FROM opportunities GROUP BY state").fetchall()
        return {s: n for s, n in rows}

    @_serialized_read
    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
