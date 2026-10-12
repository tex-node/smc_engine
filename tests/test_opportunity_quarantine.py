"""Phase 16H — opportunity-specific quarantine.

Covers: additive + idempotent quarantine operations, exclusion from active
scans, the authoritative write guard, notification suppression, per-opportunity
isolation of an identity conflict, restart persistence, preservation of the
inconsistent row's history/events, and unaffected other symbols.

All databases are disposable (tmp_path) or in-memory. No production data is
touched and no real notification transport is used.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.smc_engine.opportunity.engine import OpportunityEngine
from src.smc_engine.opportunity.models import (Opportunity, OpportunityState as St,
                                               OpportunityType as Ty,
                                               canonical_opportunity_key,
                                               opportunity_id_from_key)
from src.smc_engine.opportunity.repository import (OpportunityRepository,
                                                   QuarantinedOpportunity)

GB = "OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH"
FUTURE_EXPIRY = "2027-06-01 00:00"
NOW = "2026-12-31 00:00"


def _mk(symbol="GBPAUD", level=2.0, anchor="2026-10-12 04:00",
        state=St.OPPORTUNITY_ARMED.value, expires=FUTURE_EXPIRY, direction="BULLISH",
        otype=Ty.REVERSAL.value, anchor_kind="SWEEP"):
    k = canonical_opportunity_key(symbol, direction, otype, anchor_kind,
                                  pd.Timestamp(anchor, tz="UTC"), level)
    return Opportunity(opportunity_id=opportunity_id_from_key(k), canonical_key=k,
                       symbol=symbol, direction=direction, opportunity_type=otype,
                       state=state, created_at=f"{anchor}+00:00", updated_at=f"{anchor}+00:00",
                       expires_at=f"{expires}+00:00", sweep_time=f"{anchor}+00:00",
                       sweep_evidence={"id": "SW", "time": f"{anchor}+00:00"})


def _repo(tmp_path, name="r.db"):
    return OpportunityRepository(str(tmp_path / name))


def _view(now=NOW):
    return SimpleNamespace(m15_last_time=pd.Timestamp(now, tz="UTC"),
                           sweeps=[], csd_by_sweep={}, breaks=[], candidates=[], idms=[],
                           m15_frame=None, m15_atr=0.0, m15_obs=[], m15_fvgs=[], d1_pois=[],
                           as_of=None)


def _ids(opps):
    return {o.opportunity_id for o in opps}


# ── 1. additive, idempotent, auditable ───────────────────────────────────────

def test_quarantine_is_additive_idempotent_and_auditable(tmp_path):
    repo = _repo(tmp_path)
    assert repo.quarantined_count() == 0
    assert repo.quarantine(GB, "IDENTITY_AMBIGUOUS", {"indexed": "1.89623",
                                                      "payload": "1.89628"}) is True
    assert repo.quarantine(GB, "IDENTITY_AMBIGUOUS", {"indexed": "1.89623"}) is False
    assert repo.quarantined_count() == 1
    assert repo.is_quarantined(GB) and GB in repo.quarantined_ids()
    diag = repo.quarantine_diagnostics()
    assert diag[0]["opportunity_id"] == GB
    assert diag[0]["reason"] == "IDENTITY_AMBIGUOUS"
    assert diag[0]["evidence"]["indexed"] == "1.89623"
    assert repo.unquarantine(GB) is True
    assert repo.quarantined_count() == 0 and not repo.is_quarantined(GB)
    repo.close()


# ── 2. exclusion from active scans ───────────────────────────────────────────

def test_quarantined_excluded_from_active_scans(tmp_path):
    repo = _repo(tmp_path)
    q = _mk(level=1.9, anchor="2026-10-09 04:00")
    o = _mk(level=2.0, anchor="2026-10-12 04:00")
    repo.upsert(q)
    repo.upsert(o)
    assert _ids(repo.query(symbol="GBPAUD", active_only=True)) == {q.opportunity_id, o.opportunity_id}
    repo.quarantine(q.opportunity_id, "IDENTITY_AMBIGUOUS")
    assert _ids(repo.query(symbol="GBPAUD", active_only=True)) == {o.opportunity_id}
    assert q.opportunity_id not in _ids(repo.active_for_instrument("GBPAUD"))
    # the row is preserved for non-active (diagnostic) reads
    assert q.opportunity_id in _ids(repo.query(symbol="GBPAUD"))
    repo.close()


# ── 3. authoritative write guard ─────────────────────────────────────────────

def test_write_guard_refuses_quarantined_writes(tmp_path):
    repo = _repo(tmp_path)
    o = _mk(level=2.0, anchor="2026-10-12 04:00")
    repo.upsert(o)
    repo.quarantine(o.opportunity_id, "IDENTITY_AMBIGUOUS")
    with pytest.raises(QuarantinedOpportunity):
        repo.upsert(_mk(level=2.0, anchor="2026-10-12 04:00", state=St.READY_FOR_MITIGATION.value))
    opp = repo.get(o.opportunity_id)
    with pytest.raises(QuarantinedOpportunity):
        repo.apply_transition(opp, St.OPPORTUNITY_ARMED.value,
                              St.WAITING_FOR_CONFIRMATION.value, "X", "d",
                              "OPPORTUNITY_ADVANCED", "k", "m")
    assert repo.get(o.opportunity_id).state == St.OPPORTUNITY_ARMED.value
    repo.close()


# ── 4. isolation of an identity conflict (with and without quarantine) ───────

def _poisoned_row(repo, level="1.89623", drifted="1.89628"):
    """Reproduce the production collision: indexed key X, payload key Y, one id."""
    opp = _mk(level=1.89623, anchor="2026-10-09 04:00",
              state=St.WAITING_FOR_CONFIRMATION.value, expires="2026-10-10 04:00")
    repo.upsert(opp)
    payload = json.loads(repo._conn.execute(
        "SELECT payload_json FROM opportunities WHERE opportunity_id=?",
        (opp.opportunity_id,)).fetchone()[0])
    payload["canonical_key"] = opp.canonical_key.replace(level, drifted)
    repo._conn.execute("UPDATE opportunities SET payload_json=? WHERE opportunity_id=?",
                       (json.dumps(payload), opp.opportunity_id))
    repo._conn.commit()
    return opp


def test_identity_conflict_is_isolated_without_quarantine(tmp_path):
    repo = _repo(tmp_path)
    poisoned = _poisoned_row(repo)
    valid = _mk(level=3.0, anchor="2026-12-20 04:00", direction="BEARISH")          # distinct sweep, future expiry
    repo.upsert(valid)
    eng = OpportunityEngine(repo)
    res = eng.observe("GBPAUD", _view())                        # must NOT raise
    # the unrelated valid opportunity was processed, not aborted by the conflict
    assert repo.get(valid.opportunity_id).state != St.OPPORTUNITY_ARMED.value
    assert repo.get(poisoned.opportunity_id).state == St.WAITING_FOR_CONFIRMATION.value
    assert all(e["opportunity_id"] != poisoned.opportunity_id for e in res["events"])
    repo.close()


def test_quarantine_isolates_and_processes_other_opportunities(tmp_path):
    repo = _repo(tmp_path)
    poisoned = _poisoned_row(repo)
    valid = _mk(level=3.0, anchor="2026-12-20 04:00", direction="BEARISH")
    repo.upsert(valid)
    repo.quarantine(poisoned.opportunity_id, "IDENTITY_AMBIGUOUS_LIFECYCLE_UNRESOLVED",
                    {"indexed": "1.89623", "payload": "1.89628"})
    eng = OpportunityEngine(repo)
    res = eng.observe("GBPAUD", _view())
    assert repo.get(valid.opportunity_id).state == St.WAITING_FOR_CONFIRMATION.value
    assert repo.get(poisoned.opportunity_id).state == St.WAITING_FOR_CONFIRMATION.value
    assert all(e["opportunity_id"] != poisoned.opportunity_id for e in res["events"])
    repo.close()


# ── 5. restart persistence ───────────────────────────────────────────────────

def test_quarantine_persists_across_restart(tmp_path):
    db = str(tmp_path / "r.db")
    r1 = OpportunityRepository(db)
    o = _mk(level=2.0, anchor="2026-10-12 04:00")
    r1.upsert(o)
    r1.quarantine(o.opportunity_id, "IDENTITY_AMBIGUOUS")
    r1.close()
    r2 = OpportunityRepository(db)                              # simulated restart
    assert r2.quarantined_count() == 1 and o.opportunity_id in r2.quarantined_ids()
    assert o.opportunity_id not in _ids(r2.query(symbol="GBPAUD", active_only=True))
    r2.close()


# ── 6. mocked notification transport ─────────────────────────────────────────

def test_quarantined_event_is_not_dispatched(tmp_path):
    from src.smc_engine.web.hub import EngineHub
    from tests.test_api import FakeDemoSource
    hub = EngineHub(FakeDemoSource(), db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    calls = []

    class FakeTelegram:
        def notify(self, ev, opp):
            calls.append(ev.get("opportunity_id"))

    hub.telegram = FakeTelegram()
    hub.opportunities.repo.quarantine(GB, "IDENTITY_AMBIGUOUS")
    hub._dispatch_opportunity_events({"events": [
        {"opportunity_id": GB, "kind": "READY", "symbol": "GBPAUD", "message": "x"},
        {"opportunity_id": "OPP-OTHER", "kind": "READY", "symbol": "GBPUSD", "message": "y"},
    ]})
    assert GB not in calls
    assert "OPP-OTHER" in calls


# ── 7. preservation of the inconsistent row, history and events ──────────────

def test_quarantine_preserves_row_history_and_events(tmp_path):
    repo = _repo(tmp_path)
    o = _mk(level=1.89623, anchor="2026-10-09 04:00",
            state=St.WAITING_FOR_CONFIRMATION.value)
    repo.upsert(o)
    repo.record_state_change(o.opportunity_id, "IDLE", o.state, "armed", "SWEEP")
    repo.record_event_once(o.opportunity_id, "OPPORTUNITY_CREATED", "SWEEP:1",
                           "OPPORTUNITY_CREATED", "armed")
    snap = "SELECT canonical_key,state,payload_json FROM opportunities WHERE opportunity_id=?"
    before = repo._conn.execute(snap, (o.opportunity_id,)).fetchone()
    h0 = repo._conn.execute("SELECT COUNT(*) FROM opportunity_state_history WHERE opportunity_id=?",
                            (o.opportunity_id,)).fetchone()[0]
    e0 = repo._conn.execute("SELECT COUNT(*) FROM opportunity_events WHERE opportunity_id=?",
                            (o.opportunity_id,)).fetchone()[0]
    repo.quarantine(o.opportunity_id, "IDENTITY_AMBIGUOUS")
    after = repo._conn.execute(snap, (o.opportunity_id,)).fetchone()
    h1 = repo._conn.execute("SELECT COUNT(*) FROM opportunity_state_history WHERE opportunity_id=?",
                            (o.opportunity_id,)).fetchone()[0]
    e1 = repo._conn.execute("SELECT COUNT(*) FROM opportunity_events WHERE opportunity_id=?",
                            (o.opportunity_id,)).fetchone()[0]
    assert before == after and (h0, e0) == (h1, e1)
    repo.close()


# ── 8. other symbols continue to operate ─────────────────────────────────────

def test_other_symbols_unaffected(tmp_path):
    repo = _repo(tmp_path)
    gb = _mk(symbol="GBPAUD", level=1.9, anchor="2026-10-09 04:00")
    eu = _mk(symbol="EURUSD", level=1.1, anchor="2026-12-20 04:00")
    repo.upsert(gb)
    repo.upsert(eu)
    repo.quarantine(gb.opportunity_id, "IDENTITY_AMBIGUOUS")
    assert eu.opportunity_id in _ids(repo.query(active_only=True))
    assert gb.opportunity_id not in _ids(repo.query(active_only=True))
    OpportunityEngine(repo).observe("EURUSD", _view())          # no raise
    assert repo.get(eu.opportunity_id).state == St.WAITING_FOR_CONFIRMATION.value
    repo.close()


# ── 9. diagnostics surface the quarantine ────────────────────────────────────

def test_engine_diagnostics_expose_quarantine(tmp_path):
    repo = _repo(tmp_path)
    repo.quarantine(GB, "IDENTITY_AMBIGUOUS", {"indexed": "1.89623"})
    d = OpportunityEngine(repo).diagnostics()
    assert d["quarantined_count"] == 1
    assert d["quarantine"][0]["opportunity_id"] == GB
    repo.close()
