"""Historical R:R distribution replay tool.

Fetches bars from the live server, runs the production causal pipeline
(min_rr=0.0), records every structurally qualified setup, and writes
results to a SQLite database.  Prints a distribution report.

Usage:
    python tools/historical_rr_replay.py
    python tools/historical_rr_replay.py --symbol EURUSD --m15-bars 5000
    python tools/historical_rr_replay.py --report-only
    python tools/historical_rr_replay.py --url http://127.0.0.1:8765

All bar counts default to the maximum the server will supply.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from smc_engine.replay import ReplayRecord, distribution_report, run_replay
from smc_engine.strategy import MultiTimeframeConfig

DEFAULT_URL   = "http://127.0.0.1:8765"
DEFAULT_DB    = _ROOT / "replay_rr.db"
DEFAULT_M15   = 5000
DEFAULT_H4    = 2000
DEFAULT_D1    = 500
SYMBOLS       = ["GBPUSD", "EURUSD", "USDJPY", "XAUUSD", "GBPJPY"]


# ── bar fetch ─────────────────────────────────────────────────────────────

def fetch_bars(base_url: str, symbol: str, tf: str, count: int) -> pd.DataFrame:
    url = f"{base_url}/api/market/{symbol}?tf={tf}&count={count}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {tf} bars for {symbol}: {e}") from e
    rows = data["candles"]
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


# ── SQLite persistence ────────────────────────────────────────────────────

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS replay_records (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                TEXT    NOT NULL,
    direction             TEXT    NOT NULL,
    created_time          TEXT    NOT NULL,
    setup_id              TEXT    NOT NULL UNIQUE,
    sweep_id              TEXT    NOT NULL,
    csd_id                TEXT    NOT NULL,
    ob_id                 TEXT    NOT NULL,
    entry                 REAL    NOT NULL,
    stop                  REAL    NOT NULL,
    target                REAL    NOT NULL,
    stop_distance_pips    REAL    NOT NULL,
    target_distance_pips  REAL    NOT NULL,
    risk_reward           REAL    NOT NULL,
    irl_strength          INTEGER NOT NULL,
    irl_dist_atrs         REAL    NOT NULL,
    m15_atr               REAL    NOT NULL,
    recorded_at           TEXT    NOT NULL
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute(_CREATE_DDL)
    con.commit()
    return con


def upsert_records(con: sqlite3.Connection, records: list[ReplayRecord]) -> tuple[int, int]:
    """Insert new records; skip duplicates by setup_id. Returns (inserted, skipped)."""
    now = datetime.now(timezone.utc).isoformat()
    inserted = skipped = 0
    for r in records:
        try:
            con.execute(
                """INSERT INTO replay_records
                   (symbol, direction, created_time, setup_id, sweep_id, csd_id, ob_id,
                    entry, stop, target, stop_distance_pips, target_distance_pips,
                    risk_reward, irl_strength, irl_dist_atrs, m15_atr, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.symbol, r.direction, str(r.created_time), r.setup_id, r.sweep_id,
                 r.csd_id, r.ob_id, r.entry, r.stop, r.target,
                 r.stop_distance_pips, r.target_distance_pips,
                 r.risk_reward, r.irl_strength, r.irl_dist_atrs, r.m15_atr, now),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1
    con.commit()
    return inserted, skipped


def load_all_records(con: sqlite3.Connection) -> list[ReplayRecord]:
    rows = con.execute(
        "SELECT symbol, direction, created_time, setup_id, sweep_id, csd_id, ob_id, "
        "entry, stop, target, stop_distance_pips, target_distance_pips, "
        "risk_reward, irl_strength, irl_dist_atrs, m15_atr FROM replay_records "
        "ORDER BY created_time"
    ).fetchall()
    records = []
    for row in rows:
        r = ReplayRecord(
            symbol=row[0], direction=row[1], created_time=row[2],
            setup_id=row[3], sweep_id=row[4], csd_id=row[5], ob_id=row[6],
            entry=row[7], stop=row[8], target=row[9],
            stop_distance_pips=row[10], target_distance_pips=row[11],
            risk_reward=row[12], irl_strength=row[13],
            irl_dist_atrs=row[14], m15_atr=row[15],
        )
        records.append(r)
    return records


# ── main ──────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    con = open_db(db_path)

    if args.report_only:
        records = load_all_records(con)
        if not records:
            print("No records in database yet.  Run without --report-only to accumulate.")
            return
        print(distribution_report(records))
        print(f"\nDatabase: {db_path}  ({len(records)} total records)")
        return

    symbols = [args.symbol] if args.symbol else SYMBOLS
    total_new = total_skip = 0

    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"  {symbol}")
        print(f"{'='*60}")
        try:
            print(f"  Fetching D1 x{args.d1_bars}, H4 x{args.h4_bars}, M15 x{args.m15_bars}...")
            d1  = fetch_bars(args.url, symbol, "D1",  args.d1_bars)
            h4  = fetch_bars(args.url, symbol, "H4",  args.h4_bars)
            m15 = fetch_bars(args.url, symbol, "M15", args.m15_bars)
            print(f"  D1={len(d1)} H4={len(h4)} M15={len(m15)} bars loaded")
        except RuntimeError as e:
            print(f"  SKIP: {e}")
            continue

        cfg = MultiTimeframeConfig(min_rr=0.0)
        print("  Running causal replay (min_rr=0.0)...")
        try:
            records = run_replay(symbol, d1, h4, m15, cfg)
        except Exception as e:
            print(f"  REPLAY ERROR: {e}")
            continue

        print(f"  Found {len(records)} structurally qualified setup(s)")
        inserted, skipped = upsert_records(con, records)
        print(f"  Stored: {inserted} new, {skipped} already in DB")
        total_new += inserted
        total_skip += skipped

    print(f"\n{'='*60}")
    print(f"  Replay complete: {total_new} new records, {total_skip} duplicates skipped")
    print(f"{'='*60}")

    all_records = load_all_records(con)
    print(f"\n{distribution_report(all_records)}")
    print(f"\nDatabase: {db_path}  ({len(all_records)} total records)")

    if len(all_records) < 50:
        print(f"\nNOTE: {len(all_records)}/50 setups accumulated. "
              "Do not set min_rr > 0 until 50 setups are recorded.")
    else:
        print(f"\n{len(all_records)} setups accumulated. Threshold analysis is now valid.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url",        default=DEFAULT_URL, help="Server base URL")
    parser.add_argument("--symbol",     default="",         help="Single symbol (default: all SYMBOLS)")
    parser.add_argument("--m15-bars",   type=int, default=DEFAULT_M15, dest="m15_bars")
    parser.add_argument("--h4-bars",    type=int, default=DEFAULT_H4,  dest="h4_bars")
    parser.add_argument("--d1-bars",    type=int, default=DEFAULT_D1,  dest="d1_bars")
    parser.add_argument("--db",         default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--report-only", action="store_true", dest="report_only",
                        help="Print distribution from existing DB without fetching new data")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
