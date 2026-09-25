from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .lifecycle import SetupState
from .setup import TradeSetup


class SetupStore:
    """Small SQLite persistence boundary for strategy lifecycle metadata."""

    def __init__(self, path: str | Path = "smc_engine_state.sqlite3"):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS setups (
                setup_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                state TEXT NOT NULL,
                created_time TEXT NOT NULL,
                updated_time TEXT NOT NULL,
                ticket INTEGER,
                setup_json TEXT NOT NULL,
                reason TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                event_time TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def upsert_setup(
        self,
        setup: TradeSetup,
        state: SetupState,
        updated_time: object,
        ticket: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        payload = json.dumps(asdict(setup), default=str, sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO setups
              (setup_id,symbol,state,created_time,updated_time,ticket,setup_json,reason)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(setup_id) DO UPDATE SET
              state=excluded.state,
              updated_time=excluded.updated_time,
              ticket=COALESCE(excluded.ticket,setups.ticket),
              setup_json=excluded.setup_json,
              reason=excluded.reason
            """,
            (
                setup.id, setup.symbol, state.value, str(setup.created_time),
                str(updated_time), ticket, payload, reason,
            ),
        )
        self._conn.commit()

    def record_transition(
        self,
        setup_id: str,
        from_state: SetupState,
        to_state: SetupState,
        event_time: object,
        reason: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO lifecycle_events
              (setup_id,from_state,to_state,event_time,reason)
            VALUES (?,?,?,?,?)
            """,
            (
                setup_id, from_state.value, to_state.value,
                str(event_time), reason,
            ),
        )
        self._conn.commit()

    def get(self, setup_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT setup_id,symbol,state,created_time,updated_time,ticket,setup_json,reason "
            "FROM setups WHERE setup_id=?",
            (setup_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "setup_id": row[0],
            "symbol": row[1],
            "state": SetupState(row[2]),
            "created_time": row[3],
            "updated_time": row[4],
            "ticket": row[5],
            "setup_json": json.loads(row[6]),
            "reason": row[7],
        }

    def active(self, symbol: str) -> list[dict]:
        terminal = tuple(
            state.value for state in {
                SetupState.CLOSED,
                SetupState.POI_INVALIDATED,
                SetupState.CSD_EXPIRED,
                SetupState.OB_INVALIDATED,
                SetupState.PROTECTED_LEVEL_BREACHED,
                SetupState.ENTRY_NO_LONGER_VALID,
                SetupState.RISK_REJECTED,
                SetupState.BROKER_REJECTED,
            }
        )
        placeholders = ",".join("?" for _ in terminal)
        rows = self._conn.execute(
            f"SELECT setup_id,symbol,state,created_time,updated_time,ticket,setup_json,reason "
            f"FROM setups WHERE symbol=? AND state NOT IN ({placeholders})",
            (symbol, *terminal),
        ).fetchall()
        return [
            {
                "setup_id": row[0], "symbol": row[1], "state": SetupState(row[2]),
                "created_time": row[3], "updated_time": row[4], "ticket": row[5],
                "setup_json": json.loads(row[6]), "reason": row[7],
            }
            for row in rows
        ]

    def close(self) -> None:
        self._conn.close()
