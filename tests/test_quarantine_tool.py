"""Phase 16H.1 — the dry-run-first quarantine utility.

All databases are disposable (tmp_path). The tool must fail closed: write mode
refuses unless the database path matches the expected target, a verified backup
exists, the expected opportunity count matches, and the id resolves to exactly
one existing row. It never unquarantines and never alters the row/history/events.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3

import pandas as pd
import pytest

from src.smc_engine.opportunity.models import (Opportunity, OpportunityState as St,
                                               OpportunityType as Ty,
                                               canonical_opportunity_key,
                                               opportunity_id_from_key)
from src.smc_engine.opportunity.repository import OpportunityRepository

_TOOL = pathlib.Path(__file__).resolve().parents[1] / "tools" / "phase16g_quarantine_opportunities.py"
_spec = importlib.util.spec_from_file_location("phase16g_tool", _TOOL)
qtool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qtool)

GB = "OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH"
EVIDENCE = {"indexed_key": "REVERSAL:GBPAUD:BULLISH:SWEEP:1791518400000000000:1.89623",
            "payload_key": "REVERSAL:GBPAUD:BULLISH:SWEEP:1791518400000000000:1.89628"}


def _mk(level=1.89623):
    k = canonical_opportunity_key("GBPAUD", "BULLISH", Ty.REVERSAL.value, "SWEEP",
                                  pd.Timestamp("2026-10-09 04:00", tz="UTC"), level)
    return Opportunity(opportunity_id=opportunity_id_from_key(k), canonical_key=k,
                       symbol="GBPAUD", direction="BULLISH",
                       opportunity_type=Ty.REVERSAL.value,
                       state=St.WAITING_FOR_CONFIRMATION.value,
                       created_at="2026-10-09T04:00:00+00:00",
                       updated_at="2026-10-09T04:00:00+00:00",
                       sweep_time="2026-10-09T04:00:00+00:00",
                       sweep_evidence={"id": "SW", "time": "2026-10-09T04:00:00+00:00"})


def _db(tmp_path, name="q.db"):
    path = tmp_path / name
    repo = OpportunityRepository(str(path))
    repo.upsert(_mk())
    repo.record_state_change(GB, "IDLE", St.WAITING_FOR_CONFIRMATION.value, "armed", "SWEEP")
    repo.record_event_once(GB, "OPPORTUNITY_CREATED", "SWEEP:1", "OPPORTUNITY_CREATED", "armed")
    repo.close()
    return str(path)


def _backup(tmp_path, src, name="backup.sqlite3"):
    dst = tmp_path / name
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    s.backup(d)
    d.close()
    s.close()
    return str(dst)


def _counts(db, oid=GB):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        q = c.execute("SELECT COUNT(*) FROM opportunity_quarantine WHERE opportunity_id=?", (oid,)).fetchone()[0]
        row = c.execute("SELECT canonical_key, state, payload_json FROM opportunities WHERE opportunity_id=?", (oid,)).fetchone()
        ev = c.execute("SELECT COUNT(*) FROM opportunity_events WHERE opportunity_id=?", (oid,)).fetchone()[0]
        hi = c.execute("SELECT COUNT(*) FROM opportunity_state_history WHERE opportunity_id=?", (oid,)).fetchone()[0]
    finally:
        c.close()
    return {"quarantined": q, "row": row, "events": ev, "history": hi}


# ── dry-run is the default; no write ─────────────────────────────────────────

def test_dry_run_is_default_and_writes_nothing(tmp_path, capsys):
    db = _db(tmp_path)
    rc = qtool.main(["--db", db, "--opportunity-id", GB, "--reason", "IDENTITY_AMBIGUOUS",
                     "--evidence", json.dumps(EVIDENCE)])
    assert rc == 0
    assert "DRY-RUN" in capsys.readouterr().out
    assert _counts(db)["quarantined"] == 0


# ── fail-closed preconditions ────────────────────────────────────────────────

def test_missing_opportunity_id_is_refused(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(qtool.PreconditionError):
        qtool.assess(db, "OPP-DOES-NOT-EXIST", "R", EVIDENCE)


def test_empty_opportunity_id_is_refused(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(qtool.PreconditionError):
        qtool.assess(db, "   ", "R", EVIDENCE)


def test_missing_reason_or_evidence_is_refused(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(qtool.PreconditionError):
        qtool.assess(db, GB, "", EVIDENCE)
    with pytest.raises(qtool.PreconditionError):
        qtool.assess(db, GB, "R", {})


def test_apply_requires_expected_target_match(tmp_path):
    db = _db(tmp_path)
    plan = qtool.assess(db, GB, "R", EVIDENCE)
    with pytest.raises(qtool.PreconditionError):
        qtool.apply_quarantine(plan, expect_db=str(tmp_path / "other.db"),
                               expect_count=plan["opportunity_count"],
                               backup=_backup(tmp_path, db))
    assert _counts(db)["quarantined"] == 0


def test_apply_requires_verified_backup(tmp_path):
    db = _db(tmp_path)
    plan = qtool.assess(db, GB, "R", EVIDENCE)
    with pytest.raises(qtool.PreconditionError):
        qtool.apply_quarantine(plan, expect_db=db,
                               expect_count=plan["opportunity_count"],
                               backup=str(tmp_path / "nope.sqlite3"))
    assert _counts(db)["quarantined"] == 0


def test_apply_requires_matching_count(tmp_path):
    db = _db(tmp_path)
    plan = qtool.assess(db, GB, "R", EVIDENCE)
    with pytest.raises(qtool.PreconditionError):
        qtool.apply_quarantine(plan, expect_db=db, expect_count=999,
                               backup=_backup(tmp_path, db))
    assert _counts(db)["quarantined"] == 0


# ── idempotent apply + preservation ──────────────────────────────────────────

def test_apply_is_idempotent_and_preserves_row_history_events(tmp_path):
    db = _db(tmp_path)
    backup = _backup(tmp_path, db)
    before = _counts(db)
    plan = qtool.assess(db, GB, "IDENTITY_AMBIGUOUS", EVIDENCE)
    r1 = qtool.apply_quarantine(plan, expect_db=db,
                                expect_count=plan["opportunity_count"], backup=backup)
    assert r1["newly_quarantined"] is True and r1["quarantined"] is True
    assert r1["row_unchanged"] is True
    plan2 = qtool.assess(db, GB, "IDENTITY_AMBIGUOUS", EVIDENCE)
    r2 = qtool.apply_quarantine(plan2, expect_db=db,
                                expect_count=plan2["opportunity_count"], backup=backup)
    assert r2["newly_quarantined"] is False and r2["already_quarantined"] is True
    after = _counts(db)
    assert after["quarantined"] == 1                       # idempotent
    assert after["row"] == before["row"]                   # row/payload/key preserved
    assert (after["events"], after["history"]) == (before["events"], before["history"])


def test_tool_never_unquarantines(tmp_path):
    db = _db(tmp_path)
    backup = _backup(tmp_path, db)
    plan = qtool.assess(db, GB, "R", EVIDENCE)
    qtool.apply_quarantine(plan, expect_db=db, expect_count=plan["opportunity_count"], backup=backup)
    # re-running the utility (even dry-run) must not remove the quarantine
    rc = qtool.main(["--db", db, "--opportunity-id", GB, "--reason", "R",
                     "--evidence", json.dumps(EVIDENCE)])
    assert rc == 0
    assert _counts(db)["quarantined"] == 1
