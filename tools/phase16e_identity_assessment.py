"""Phase 16E — read-only canonical-identity / lifecycle migration assessment.

Takes a consistent snapshot of the operational database via the SQLite backup
API and reports identity/lifecycle inconsistencies. It NEVER writes to the
source database and never mutates the snapshot.

Usage:
    python tools/phase16e_identity_assessment.py [source_db] [--json]

Findings:
  * rows where the indexed canonical_key disagrees with payload.canonical_key
  * rows whose derived opportunity_id no longer matches the payload key
  * terminal rows with a LATER non-terminal state transition (reactivation)
  * lifecycle state-history transitions with no corresponding event row
  * potential duplicate opportunities under a level-inclusive id scheme
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

TERMINAL = {"INVALIDATED", "EXPIRED", "TERMINAL", "ENTRY_TRIGGERED", "SUPERSEDED"}
DEFAULT_DB = r"C:\smc_engine\smc_engine_gui.sqlite3"


def snapshot(source: str) -> str:
    out_dir = os.path.join(tempfile.gettempdir(), "smc16e")
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=15.0)
    dst = sqlite3.connect(dest)
    src.backup(dst)
    dst.close()
    src.close()
    return dest


def derived_id_from_key(key: str) -> str:
    parts = key.split(":")
    otype, symbol, direction, anchor_kind = parts[0], parts[1], parts[2], parts[3]
    return f"OPP-{otype}-{symbol}-{anchor_kind}-{parts[4]}-{direction}"


def assess(db: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT opportunity_id, canonical_key, state, payload_json FROM opportunities").fetchall()

    key_mismatch, id_drift = [], []
    for oid, colkey, state, pj in rows:
        try:
            payload = json.loads(pj)
        except Exception:
            payload = {}
        pkey = payload.get("canonical_key")
        if pkey is not None and pkey != colkey:
            key_mismatch.append({"opportunity_id": oid, "state": state,
                                 "column_key": colkey, "payload_key": pkey})
        if pkey is not None and derived_id_from_key(pkey) != oid:
            id_drift.append({"opportunity_id": oid, "payload_key": pkey})

    # Terminal rows later reactivated to a non-terminal state.
    hist: dict[str, list] = {}
    for oid, to_state, hid in cur.execute(
            "SELECT opportunity_id, to_state, id FROM opportunity_state_history ORDER BY id ASC"):
        hist.setdefault(oid, []).append(to_state)
    reactivated = []
    for oid, seq in hist.items():
        seen_term = False
        for ts in seq:
            if ts in TERMINAL:
                seen_term = True
            elif seen_term:
                reactivated.append({"opportunity_id": oid, "later_state": ts})
                break

    # Transitions vs events per opportunity (proxy for unrecorded transitions).
    hist_counts = {oid: len(seq) for oid, seq in hist.items()}
    evt_counts = {}
    for oid, n in cur.execute(
            "SELECT opportunity_id, COUNT(*) FROM opportunity_events GROUP BY opportunity_id"):
        evt_counts[oid] = n
    unrecorded = []
    for oid, hc in hist_counts.items():
        # IDLE->ARMED is the creation history; events include OPPORTUNITY_CREATED.
        if hc > evt_counts.get(oid, 0):
            unrecorded.append({"opportunity_id": oid, "history_rows": hc,
                               "event_rows": evt_counts.get(oid, 0)})

    # Duplicate opportunities under a level-inclusive (full key) id scheme:
    # the legacy id drops the level, so group by the key without the level.
    groups: dict[str, int] = {}
    for oid, colkey, state, pj in rows:
        parts = colkey.split(":")
        if len(parts) >= 6:
            groups[":".join(parts[:5])] = groups.get(":".join(parts[:5]), 0) + 1
    potential_dupes = {k: v for k, v in groups.items() if v > 1}

    total = len(rows)
    con.close()
    return {
        "total_opportunities": total,
        "key_mismatch_count": len(key_mismatch),
        "key_mismatch": key_mismatch[:50],
        "id_drift_count": len(id_drift),
        "id_drift": id_drift[:50],
        "reactivation_count": len(reactivated),
        "reactivation": reactivated[:50],
        "history_gt_events_count": len(unrecorded),
        "history_gt_events": unrecorded[:50],
        "potential_duplicate_groups": potential_dupes,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    source = args[0] if args else DEFAULT_DB
    snap = snapshot(source)
    result = assess(snap)
    result["source_db"] = source
    result["snapshot_db"] = snap
    result["assessed_at"] = datetime.now(timezone.utc).isoformat()
    if "--json" in sys.argv:
        print(json.dumps(result, indent=2, default=str))
    else:
        for k, v in result.items():
            if k in ("key_mismatch", "id_drift", "reactivation", "history_gt_events", "potential_duplicate_groups"):
                continue
            print(f"{k}: {v}")
        print("key_mismatch (rows where column key != payload key):")
        for m in result["key_mismatch"]:
            print("  -", json.dumps(m, default=str))
        print("reactivation (terminal -> later non-terminal):")
        for m in result["reactivation"]:
            print("  -", json.dumps(m, default=str))
        print("potential duplicate groups (same key without level):")
        for k, v in list(result["potential_duplicate_groups"].items())[:20]:
            print(f"  - {k} => {v}")


if __name__ == "__main__":
    main()
