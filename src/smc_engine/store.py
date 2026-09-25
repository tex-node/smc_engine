from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from .lifecycle import SetupState
from .setup import TradeSetup
from .models import Direction


class SetupStore:
    """SQLite persistence boundary with transactional lifecycle writes."""

    SCHEMA_VERSION = 3

    def __init__(self, path: str | Path = "smc_engine_state.sqlite3"):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS setups (
                setup_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                state TEXT NOT NULL,
                created_time TEXT NOT NULL,
                updated_time TEXT NOT NULL,
                ticket INTEGER,
                setup_json TEXT NOT NULL,
                reason TEXT
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                event_time TEXT NOT NULL,
                reason TEXT NOT NULL
            )"""
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(setups)").fetchall()}
        if "position_ticket" not in columns:
            self._conn.execute("ALTER TABLE setups ADD COLUMN position_ticket INTEGER")

        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_setups_symbol_state ON setups(symbol, state)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_setup_time ON lifecycle_events(setup_id, event_time)")
        self._conn.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(self.SCHEMA_VERSION),),
        )
        self._conn.commit()

    @staticmethod
    def _json_default(value):
        if isinstance(value, Enum):
            return value.value
        return str(value)

    def _setup_payload(self, setup: TradeSetup) -> str:
        return json.dumps(asdict(setup), default=self._json_default, sort_keys=True)

    def _upsert_setup_no_commit(self, setup, state, updated_time, ticket=None, reason=None, position_ticket=None):
        self._conn.execute(
            """INSERT INTO setups
              (setup_id,symbol,state,created_time,updated_time,ticket,setup_json,reason)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(setup_id) DO UPDATE SET
              state=excluded.state,
              updated_time=excluded.updated_time,
              ticket=COALESCE(excluded.ticket,setups.ticket),
              position_ticket=COALESCE(excluded.position_ticket,setups.position_ticket),
              setup_json=excluded.setup_json,
              reason=excluded.reason""",
            (setup.id, setup.symbol, state.value, str(setup.created_time),
             str(updated_time), ticket, self._setup_payload(setup), reason),
        )

    def upsert_setup(self, setup, state, updated_time, ticket: Optional[int] = None, reason: Optional[str] = None) -> None:
        with self._conn:
            self._upsert_setup_no_commit(setup, state, updated_time, ticket, reason)

    def record_transition(self, setup_id, from_state, to_state, event_time, reason):
        with self._conn:
            self._conn.execute(
                """INSERT INTO lifecycle_events
                   (setup_id,from_state,to_state,event_time,reason)
                   VALUES (?,?,?,?,?)""",
                (setup_id, from_state.value, to_state.value, str(event_time), reason),
            )

    def persist_transition(self, setup, from_state, to_state, event_time, reason, ticket=None, position_ticket=None):
        """Atomically persist the new setup state, broker identity, and transition event."""
        with self._conn:
            self._upsert_setup_no_commit(setup, to_state, event_time, ticket, reason, position_ticket=position_ticket)
            self._conn.execute(
                """INSERT INTO lifecycle_events
                   (setup_id,from_state,to_state,event_time,reason)
                   VALUES (?,?,?,?,?)""",
                (setup.id, from_state.value, to_state.value, str(event_time), reason),
            )

    @staticmethod
    def setup_from_json(payload: dict) -> TradeSetup:
        data = dict(payload)
        data["direction"] = Direction(data["direction"])
        return TradeSetup(**data)

    def load_setup(self, setup_id: str) -> Optional[TradeSetup]:
        row = self.get(setup_id)
        if row is None:
            return None
        return self.setup_from_json(row["setup_json"])

    def get(self, setup_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT setup_id,symbol,state,created_time,updated_time,ticket,position_ticket,setup_json,reason FROM setups WHERE setup_id=?",
            (setup_id,),
        ).fetchone()
        if row is None:
            return None
        return {"setup_id": row[0], "symbol": row[1], "state": SetupState(row[2]),
                "created_time": row[3], "updated_time": row[4], "ticket": row[5], "position_ticket": row[6],
                "setup_json": json.loads(row[7]), "reason": row[8]}

    def active(self, symbol: str) -> list[dict]:
        terminal = tuple(state.value for state in {
            SetupState.CLOSED, SetupState.POI_INVALIDATED, SetupState.CSD_EXPIRED,
            SetupState.OB_INVALIDATED, SetupState.PROTECTED_LEVEL_BREACHED,
            SetupState.ENTRY_NO_LONGER_VALID, SetupState.RISK_REJECTED,
            SetupState.BROKER_REJECTED,
        })
        placeholders = ",".join("?" for _ in terminal)
        rows = self._conn.execute(
            f"SELECT setup_id,symbol,state,created_time,updated_time,ticket,position_ticket,setup_json,reason "
            f"FROM setups WHERE symbol=? AND state NOT IN ({placeholders})",
            (symbol, *terminal),
        ).fetchall()
        return [{"setup_id": r[0], "symbol": r[1], "state": SetupState(r[2]),
                 "created_time": r[3], "updated_time": r[4], "ticket": r[5], "position_ticket": r[6],
                 "setup_json": json.loads(r[7]), "reason": r[8]} for r in rows]

    def set_position_ticket(self, setup_id: str, position_ticket: int) -> None:
        with self._conn:
            updated = self._conn.execute(
                "UPDATE setups SET position_ticket=? WHERE setup_id=?",
                (int(position_ticket), setup_id),
            ).rowcount
            if updated != 1:
                raise ValueError(f"Unknown setup: {setup_id}")

    def close(self) -> None:
        self._conn.close()
