"""Opportunity persistence (GUI-owned SQLite, same database as setup history).

Additive migration only: CREATE TABLE IF NOT EXISTS, no destructive changes to
existing setup/history/lifecycle tables. Restart recovery = load_active().
Alert idempotency = UNIQUE(opportunity_id, transition, evidence_key).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict
from typing import Optional

import pandas as pd

from .models import Opportunity
from .state_machine import is_terminal
from ..web.dbwrite import serialized_write
from ..web.dbwrite import serialized_read as _serialized_read


def _utcnow() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def identity_fingerprint(canonical_key: str) -> str:
    """Stable, non-reversible short fingerprint of a canonical identity.

    Used in collision diagnostics so an operator can correlate the affected row
    without logging anything sensitive (the key is symbols/levels only, but a
    fingerprint keeps diagnostics compact and diff-friendly)."""
    return hashlib.sha256(str(canonical_key).encode("utf-8")).hexdigest()[:12]


class OpportunityInvariantError(RuntimeError):
    """Base class for authoritative identity/lifecycle invariant violations."""


class OpportunityIdentityConflict(OpportunityInvariantError):
    """An opportunity_id already refers to a different canonical identity.

    Raised BEFORE any write so the existing row is never overwritten.
    """


class TerminalStateImmutable(OpportunityInvariantError):
    """A terminal opportunity may not be silently reactivated via a write."""


class QuarantinedOpportunity(OpportunityInvariantError):
    """A quarantined opportunity may not be written, re-armed, or transitioned."""


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
        CREATE TABLE IF NOT EXISTS opportunity_quarantine(
          opportunity_id TEXT PRIMARY KEY,
          reason TEXT NOT NULL,
          at TEXT NOT NULL,
          evidence_json TEXT
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
        # Bounded, safe identity/lifecycle diagnostics (never sensitive).
        self._identity_diags: list = []

    # ---------- identity / lifecycle invariants ----------
    def _row_identity(self, opportunity_id: str):
        """(canonical_key, state) of the persisted row, or None if absent."""
        r = self._conn.execute(
            "SELECT canonical_key, state FROM opportunities WHERE opportunity_id=?",
            (opportunity_id,)).fetchone()
        return (r[0], r[1]) if r else None

    def _record_identity_diag(self, kind: str, opportunity_id: str,
                              incoming_key: str, existing_key: Optional[str] = None,
                              existing_state: Optional[str] = None) -> None:
        self._identity_diags.append({
            "kind": kind,
            "opportunity_id": opportunity_id,
            "incoming_key_fp": identity_fingerprint(incoming_key),
            "existing_key_fp": identity_fingerprint(existing_key) if existing_key else None,
            "existing_state": existing_state,
            "at": _utcnow(),
        })
        if len(self._identity_diags) > 200:
            del self._identity_diags[0]

    def _assert_identity_invariants(self, opp: Opportunity) -> None:
        """Enforce, at the authoritative write boundary:
          1. a quarantined opportunity is never written;
          2. an opportunity_id refers to exactly one canonical identity;
          3. a terminal row cannot be silently changed to another state.
        """
        if self._is_quarantined(opp.opportunity_id):
            self._record_identity_diag("QUARANTINED", opp.opportunity_id, opp.canonical_key)
            raise QuarantinedOpportunity(
                f"opportunity {opp.opportunity_id!r} is quarantined; write refused")
        existing = self._row_identity(opp.opportunity_id)
        if existing is None:
            return
        existing_key, existing_state = existing
        if existing_key != opp.canonical_key:
            self._record_identity_diag("IDENTITY_COLLISION", opp.opportunity_id,
                                       opp.canonical_key, existing_key, existing_state)
            raise OpportunityIdentityConflict(
                f"opportunity_id {opp.opportunity_id!r} is bound to a different "
                f"canonical identity (existing_fp="
                f"{identity_fingerprint(existing_key)}, "
                f"incoming_fp={identity_fingerprint(opp.canonical_key)})")
        if is_terminal(existing_state) and not is_terminal(opp.state):
            # Reactivating a terminal row (terminal -> non-terminal) is forbidden.
            # Terminal -> terminal reclassification is allowed (lifecycle contract).
            self._record_identity_diag("TERMINAL_IMMUTABILITY", opp.opportunity_id,
                                       opp.canonical_key, existing_key, existing_state)
            raise TerminalStateImmutable(
                f"terminal opportunity {opp.opportunity_id!r} ({existing_state}) "
                f"cannot be reactivated to active state {opp.state}")

    def identity_diagnostics(self) -> list:
        with self._lock:
            return list(self._identity_diags)

    def reset_identity_diagnostics(self) -> None:
        with self._lock:
            self._identity_diags = []

    # ---------- quarantine (opportunity-specific, auditable) ----------
    @serialized_write
    def quarantine(self, opportunity_id: str, reason: str,
                   evidence: Optional[dict] = None) -> bool:
        """Idempotently mark ONE opportunity quarantined. True if newly added.

        Quarantined opportunities are excluded from active scans and refused at
        the write boundary; the row, its events and its history are untouched.
        """
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO opportunity_quarantine
                   (opportunity_id, reason, at, evidence_json) VALUES(?,?,?,?)""",
                (opportunity_id, reason, _utcnow(),
                 json.dumps(evidence or {}, default=str, sort_keys=True)))
            self._conn.commit()
            return cur.rowcount > 0

    @serialized_write
    def unquarantine(self, opportunity_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM opportunity_quarantine WHERE opportunity_id=?",
                (opportunity_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def _is_quarantined(self, opportunity_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM opportunity_quarantine WHERE opportunity_id=?",
            (opportunity_id,)).fetchone() is not None

    @_serialized_read
    def is_quarantined(self, opportunity_id: str) -> bool:
        return self._is_quarantined(opportunity_id)

    @_serialized_read
    def quarantined_ids(self) -> set:
        return {r[0] for r in self._conn.execute(
            "SELECT opportunity_id FROM opportunity_quarantine")}

    @_serialized_read
    def quarantine_diagnostics(self) -> list:
        rows = self._conn.execute(
            "SELECT opportunity_id, reason, at, evidence_json "
            "FROM opportunity_quarantine ORDER BY at, opportunity_id").fetchall()
        out = []
        for oid, reason, at, ev in rows:
            try:
                evidence = json.loads(ev) if ev else {}
            except (TypeError, ValueError):
                evidence = {}
            out.append({"opportunity_id": oid, "reason": reason, "at": at,
                        "evidence": evidence})
        return out

    @_serialized_read
    def quarantined_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM opportunity_quarantine").fetchone()[0]

    # ---------- write ----------
    @serialized_write
    def upsert(self, opp: Opportunity) -> None:
        d = asdict(opp)
        with self._lock:
            self._assert_identity_invariants(opp)
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
    def apply_transition(self, opp: Opportunity, from_state: str, to_state: str,
                         reason: str, detail: str, kind: str,
                         evidence_key: str, message: str) -> bool:
        """Atomically persist a state change + its state-history row + its
        lifecycle event in a single transaction.

        Returns True when the event is newly recorded; False when it is an
        idempotent replay of the SAME logical transition
        (opportunity_id, transition, evidence_key). Identity and terminal-
        immutability invariants are enforced before any write.
        """
        opp.state = to_state
        d = asdict(opp)
        if from_state == to_state:
            # No state change -> no transition, no history row, no event.
            return False
        with self._lock:
            self._assert_identity_invariants(opp)
            try:
                self._conn.execute(
                    """UPDATE opportunities SET
                         state=?, updated_at=?, expires_at=?, last_seen=?,
                         setup_id=?, risk_status=?, reason=?, blocker=?,
                         next_expected=?, payload_json=?
                       WHERE opportunity_id=?""",
                    (to_state, str(opp.updated_at),
                     str(opp.expires_at) if opp.expires_at else None,
                     str(opp.last_seen), opp.setup_id, opp.risk_status,
                     opp.reason, opp.blocker, opp.next_expected,
                     json.dumps(d, default=str, sort_keys=True),
                     opp.opportunity_id))
                self._conn.execute(
                    """INSERT INTO opportunity_state_history
                       (opportunity_id, at, from_state, to_state, reason, detail)
                       VALUES(?,?,?,?,?,?)""",
                    (opp.opportunity_id, _utcnow(), from_state, to_state, reason, detail))
                # Dedup identity includes the concrete transition (from->to), not
                # just the event kind, so a genuinely NEW transition is never
                # suppressed merely because an old evidence_key was reused; only
                # a true replay of the same transition+evidence is suppressed.
                transition_token = f"{from_state}->{to_state}"
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO opportunity_events
                       (opportunity_id, at, transition, evidence_key, kind, message)
                       VALUES(?,?,?,?,?,?)""",
                    (opp.opportunity_id, _utcnow(), transition_token,
                     evidence_key, kind, message))
                fresh = cur.rowcount > 0
                self._conn.commit()
                return fresh
            except Exception:
                self._conn.rollback()
                raise

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
                    "('INVALIDATED','EXPIRED','TERMINAL','ENTRY_TRIGGERED','SUPERSEDED')"
                    " AND opportunity_id NOT IN "
                    "(SELECT opportunity_id FROM opportunity_quarantine)")
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
               "AND opportunity_id NOT IN "
               "(SELECT opportunity_id FROM opportunity_quarantine) "
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
