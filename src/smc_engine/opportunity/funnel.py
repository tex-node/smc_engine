"""Pre-arm funnel persistence and aggregation.

Stores one row per FunnelEvent emitted by trace_chain() so the pre-arm
causal chain is observable without creating opportunity records for rejections.

Additive migration only: CREATE TABLE IF NOT EXISTS scan_funnel_events.
No changes to any existing table.
"""
from __future__ import annotations

import sqlite3
import threading
from enum import Enum
from typing import Optional

import pandas as pd

from ..causal import FunnelEvent
from ..web.dbwrite import serialized_write
from ..web.dbwrite import serialized_read as _serialized_read


class FunnelStage(str, Enum):
    D1_POI = "D1_POI"
    H4_SWEEP = "H4_SWEEP"
    H4_CSD = "H4_CSD"
    M15_OB = "M15_OB"
    M15_MITIGATION = "M15_MITIGATION"
    M15_IDM = "M15_IDM"
    IRL = "IRL"
    READY = "READY"


class FunnelReason(str, Enum):
    # D1 POI stage
    NO_D1_POI = "NO_D1_POI"
    D1_POI_INVALID = "D1_POI_INVALID"
    D1_POI_CONSUMED = "D1_POI_CONSUMED"
    D1_POI_DIRECTION_MISMATCH = "D1_POI_DIRECTION_MISMATCH"
    D1_POI_TOO_OLD = "D1_POI_TOO_OLD"
    # HTF context stage
    NO_HTF_CONTEXT = "NO_HTF_CONTEXT"
    HTF_BIAS_MISMATCH = "HTF_BIAS_MISMATCH"
    # Sweep stage
    NO_SWEEP = "NO_SWEEP"
    SWEEP_INVALID = "SWEEP_INVALID"
    SWEEP_WINDOW_EXPIRED = "SWEEP_WINDOW_EXPIRED"
    # CSD stage
    NO_CSD = "NO_CSD"
    CSD_DIRECTION_MISMATCH = "CSD_DIRECTION_MISMATCH"
    CSD_WINDOW_EXPIRED = "CSD_WINDOW_EXPIRED"
    # Post-CSD POI / OB stage
    NO_POST_CSD_POI = "NO_POST_CSD_POI"
    POI_DIRECTION_MISMATCH = "POI_DIRECTION_MISMATCH"
    POI_TOO_OLD = "POI_TOO_OLD"
    POI_CONSUMED = "POI_CONSUMED"
    POI_TOO_SMALL = "POI_TOO_SMALL"
    # IDM stage
    NO_IDM = "NO_IDM"
    IDM_WINDOW_EXPIRED = "IDM_WINDOW_EXPIRED"
    # IRL / structural stage
    IRL_MISSING = "IRL_MISSING"
    # Advancement / success reasons
    ADVANCED = "ADVANCED"
    RR_FILTERED = "RR_FILTERED"
    EXECUTION_READY = "EXECUTION_READY"
    SETUP_BUILD_FAILED = "SETUP_BUILD_FAILED"
    # Lifecycle / arbitration
    CONFLICT_SUPERSEDED = "CONFLICT_SUPERSEDED"
    DUPLICATE = "DUPLICATE"


def _utcnow() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


class FunnelRepository:
    """Append-only SQLite store for pre-arm funnel events.

    One row per FunnelEvent. Never modifies opportunity records or any other table.
    """

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_funnel_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL,
          scan_timestamp TEXT NOT NULL,
          market_event_timestamp TEXT NOT NULL,
          stage TEXT NOT NULL,
          reason TEXT NOT NULL,
          direction TEXT NOT NULL,
          causal_anchor_ref TEXT NOT NULL DEFAULT '',
          evidence_timestamp TEXT NOT NULL DEFAULT '',
          config_fingerprint TEXT NOT NULL DEFAULT '',
          inserted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_funnel_symbol
          ON scan_funnel_events(symbol, scan_timestamp);
        CREATE INDEX IF NOT EXISTS idx_funnel_reason
          ON scan_funnel_events(reason, scan_timestamp);
        """)
        self._conn.commit()
        self._lock = threading.RLock()

    @serialized_write
    def persist_events(self, events: list[FunnelEvent]) -> None:
        """Insert a batch of FunnelEvents. No-op if events is empty."""
        if not events:
            return
        now = _utcnow()
        rows = [
            (e.symbol, e.scan_timestamp, e.market_event_timestamp,
             e.stage, e.reason, e.direction,
             e.causal_anchor_ref, e.evidence_timestamp, e.config_fingerprint,
             now)
            for e in events
        ]
        with self._lock:
            self._conn.executemany(
                """INSERT INTO scan_funnel_events
                   (symbol, scan_timestamp, market_event_timestamp, stage, reason,
                    direction, causal_anchor_ref, evidence_timestamp,
                    config_fingerprint, inserted_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()

    @_serialized_read
    def recent_events(self, symbol: Optional[str] = None,
                      limit: int = 500) -> list[dict]:
        """Return the most recent funnel events, optionally filtered by symbol."""
        with self._lock:
            if symbol:
                rows = self._conn.execute(
                    """SELECT symbol, scan_timestamp, market_event_timestamp,
                              stage, reason, direction, causal_anchor_ref,
                              evidence_timestamp, config_fingerprint, inserted_at
                       FROM scan_funnel_events
                       WHERE symbol = ?
                       ORDER BY id DESC LIMIT ?""",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT symbol, scan_timestamp, market_event_timestamp,
                              stage, reason, direction, causal_anchor_ref,
                              evidence_timestamp, config_fingerprint, inserted_at
                       FROM scan_funnel_events
                       ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        cols = ["symbol", "scan_timestamp", "market_event_timestamp",
                "stage", "reason", "direction", "causal_anchor_ref",
                "evidence_timestamp", "config_fingerprint", "inserted_at"]
        return [dict(zip(cols, row)) for row in rows]

    @_serialized_read
    def aggregate(self, symbol: Optional[str] = None,
                  since: Optional[str] = None) -> list[dict]:
        """Return per-reason counts, optionally filtered by symbol and/or time."""
        params: list = []
        where_clauses: list[str] = []
        if symbol:
            where_clauses.append("symbol = ?")
            params.append(symbol)
        if since:
            where_clauses.append("scan_timestamp >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT symbol, stage, reason, direction, COUNT(*) as count
                    FROM scan_funnel_events
                    {where}
                    GROUP BY symbol, stage, reason, direction
                    ORDER BY count DESC""",
                params,
            ).fetchall()
        cols = ["symbol", "stage", "reason", "direction", "count"]
        return [dict(zip(cols, row)) for row in rows]
