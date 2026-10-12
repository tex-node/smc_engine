"""Phase 16G/16H — opportunity quarantine utility (DRY-RUN BY DEFAULT).

Marks ONE explicitly supplied opportunity as quarantined through the
authoritative repository (additive `opportunity_quarantine` table). It never
touches the opportunity row, its payload, its canonical keys, its lifecycle
history, or its event history, and it never unquarantines.

Fail-closed preconditions (write mode refuses if any is unmet):
  * explicit database path and explicit opportunity id;
  * explicit, non-empty reason and supporting evidence (JSON object);
  * the opportunity id resolves to exactly one existing row;
  * the resolved database path equals the caller's expected target;
  * a caller-supplied backup exists and passes PRAGMA integrity_check;
  * the caller-supplied expected opportunity count matches the database.

Usage (dry-run is the default; no write happens):
    python tools/phase16g_quarantine_opportunities.py \
        --db C:\\smc_engine\\smc_engine_gui.sqlite3 \
        --opportunity-id OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH \
        --reason IDENTITY_AMBIGUOUS_LIFECYCLE_UNRESOLVED \
        --evidence '{"indexed_key":"...1.89623","payload_key":"...1.89628"}'

Write mode (only after explicit approval; all confirmations required):
    python tools/phase16g_quarantine_opportunities.py \
        --db C:\\smc_engine\\smc_engine_gui.sqlite3 \
        --expect-db C:\\smc_engine\\smc_engine_gui.sqlite3 \
        --backup C:\\smc_engine\\backups\\smc_engine_gui_<ts>.sqlite3 \
        --expect-count 186 \
        --opportunity-id OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH \
        --reason IDENTITY_AMBIGUOUS_LIFECYCLE_UNRESOLVED \
        --evidence '{"indexed_key":"...1.89623","payload_key":"...1.89628"}' \
        --apply

Required confirmations for write mode: --apply, --expect-db (must canonicalize
to the same file as --db), --backup (a verified copy taken before the change),
and --expect-count (pins the database generation). Missing any one aborts
without writing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


class PreconditionError(RuntimeError):
    """A required safety precondition was not met (write refused)."""


def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _open_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15.0)


def integrity_ok(path: str) -> bool:
    """True only if the file opens read-only and PRAGMA integrity_check == ok."""
    if not os.path.isfile(path):
        return False
    conn = _open_ro(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def _row_snapshot(conn: sqlite3.Connection, opportunity_id: str):
    try:
        row = conn.execute(
            "SELECT canonical_key, state, payload_json FROM opportunities "
            "WHERE opportunity_id=?", (opportunity_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        raise PreconditionError(f"opportunities table unavailable: {exc}") from exc
    if row is None:
        return None
    ck, state, payload = row
    return {
        "opportunity_id": opportunity_id,
        "canonical_key": ck,
        "state": state,
        "payload_sha256": hashlib.sha256((payload or "").encode("utf-8")).hexdigest()[:16],
    }


def assess(db: str, opportunity_id: str, reason: str, evidence: dict) -> dict:
    """Read-only preconditions + a quarantine plan. Raises PreconditionError."""
    if not db or not str(db).strip():
        raise PreconditionError("an explicit --db database path is required")
    if not opportunity_id or not str(opportunity_id).strip():
        raise PreconditionError("an explicit --opportunity-id is required")
    if not reason or not str(reason).strip():
        raise PreconditionError("an explicit non-empty --reason is required")
    if not isinstance(evidence, dict) or not evidence:
        raise PreconditionError("non-empty --evidence (JSON object) is required")
    if not os.path.isfile(db):
        raise PreconditionError(f"database not found: {db}")

    conn = _open_ro(db)
    try:
        snapshot = _row_snapshot(conn, opportunity_id)
        total = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    finally:
        conn.close()
    if snapshot is None:
        raise PreconditionError(
            f"opportunity id missing or ambiguous (no row): {opportunity_id}")
    return {
        "database": os.path.abspath(db),
        "database_canonical": _canonical(db),
        "opportunity": snapshot,
        "opportunity_count": total,
        "reason": reason,
        "evidence": evidence,
    }


def apply_quarantine(plan: dict, expect_db: str, expect_count: int,
                     backup: str) -> dict:
    """Write mode: enforce every confirmation, then quarantine (idempotent)."""
    if not expect_db:
        raise PreconditionError("--expect-db is required in write mode")
    if _canonical(expect_db) != plan["database_canonical"]:
        raise PreconditionError(
            f"resolved database {plan['database']!r} does not match the expected "
            f"target {os.path.abspath(expect_db)!r}")
    if not backup or not os.path.isfile(backup):
        raise PreconditionError("--backup must name an existing backup file")
    if not integrity_ok(backup):
        raise PreconditionError(f"backup failed integrity_check: {backup}")
    if expect_count is None or int(expect_count) != int(plan["opportunity_count"]):
        raise PreconditionError(
            f"expected opportunity count {expect_count} does not match the "
            f"database ({plan['opportunity_count']})")

    from src.smc_engine.opportunity.repository import OpportunityRepository
    repo = OpportunityRepository(plan["database"])
    try:
        before = repo._conn.execute(
            "SELECT canonical_key, state, payload_json FROM opportunities "
            "WHERE opportunity_id=?", (plan["opportunity"]["opportunity_id"],)).fetchone()
        added = repo.quarantine(plan["opportunity"]["opportunity_id"],
                                plan["reason"], plan["evidence"])
        after = repo._conn.execute(
            "SELECT canonical_key, state, payload_json FROM opportunities "
            "WHERE opportunity_id=?", (plan["opportunity"]["opportunity_id"],)).fetchone()
        events = repo._conn.execute(
            "SELECT COUNT(*) FROM opportunity_events WHERE opportunity_id=?",
            (plan["opportunity"]["opportunity_id"],)).fetchone()[0]
        history = repo._conn.execute(
            "SELECT COUNT(*) FROM opportunity_state_history WHERE opportunity_id=?",
            (plan["opportunity"]["opportunity_id"],)).fetchone()[0]
        quarantined = repo.is_quarantined(plan["opportunity"]["opportunity_id"])
    finally:
        repo.close()

    if before != after:
        raise PreconditionError("opportunity row changed during quarantine (aborted)")
    return {
        "applied": True,
        "newly_quarantined": added,
        "already_quarantined": not added,
        "quarantined": quarantined,
        "row_unchanged": before == after,
        "events": events,
        "history": history,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _parse_evidence(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PreconditionError(f"--evidence is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PreconditionError("--evidence must be a JSON object")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Quarantine one opportunity (dry-run by default).")
    parser.add_argument("--db", required=True, help="explicit database path")
    parser.add_argument("--opportunity-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence", required=True, help="JSON object")
    parser.add_argument("--apply", action="store_true",
                        help="perform the write (default is dry-run)")
    parser.add_argument("--expect-db", default="")
    parser.add_argument("--backup", default="")
    parser.add_argument("--expect-count", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        evidence = _parse_evidence(args.evidence)
        plan = assess(args.db, args.opportunity_id, args.reason, evidence)
        if not args.apply:
            print("DRY-RUN (no write). Plan:")
            print(json.dumps(plan, indent=2, default=str))
            print("\nTo apply, re-run with --apply --expect-db <path> "
                  "--backup <verified-copy> --expect-count <n>.")
            return 0
        result = apply_quarantine(plan, args.expect_db, args.expect_count, args.backup)
        print(json.dumps({"plan": plan, "result": result}, indent=2, default=str))
        return 0
    except PreconditionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
