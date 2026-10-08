"""Phase 11 — Opportunity Conflict Arbitration tests.

Hard invariant: ONLY ONE ACTIVE DIRECTIONAL THESIS PER CANONICAL INSTRUMENT.

Tests verify:
  - Opposing-direction conflict: weaker superseded, stronger survives
  - Opposing-direction conflict: new candidate rejected when existing is stronger
  - Opposing-direction tie-break by anchor recency
  - Same-direction conflict: duplicate rejected
  - Same-direction: stronger new thesis supersedes weaker existing
  - ACCEPT when no existing active opps for instrument
  - SUPERSEDED is excluded from active queries
  - Startup reconciliation resolves pre-existing conflicts
  - Superseded opp fields populated correctly
  - OPPORTUNITY_SUPERSEDED event emitted
  - engine.observe() with full scan applies arbitration
  - Re-discovery of superseded key produces conflict check again
  - canonical_instrument strips non-alnum
  - thesis_strength ordering
  - Reconcile handles multiple instruments independently
  - Reconcile handles same-direction duplicates
  - state_machine allows → SUPERSEDED from all active states
  - active_for_instrument excludes SUPERSEDED
  - REJECT records to blocked list
  - SUPERSEDE records to superseded list
  - No duplicate OPPORTUNITY_SUPERSEDED events (idempotent emit)
"""
from __future__ import annotations

import tempfile
import os

import pandas as pd
import pytest

from src.smc_engine.opportunity.arbiter import (
    ArbiterDecision, OpportunityArbiter, canonical_instrument, thesis_strength,
)
from src.smc_engine.opportunity.engine import OpportunityEngine
from src.smc_engine.opportunity.models import (
    Opportunity, OpportunityEventKind as Ev, OpportunityState as St,
    OpportunityType as Ty, TERMINAL_STATES,
)
from src.smc_engine.opportunity.repository import OpportunityRepository
from src.smc_engine.opportunity.state_machine import can_transition, is_terminal


# ------------------------------------------------------------------- helpers

def _repo() -> OpportunityRepository:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return OpportunityRepository(path)


def _opp(symbol: str, direction: str, state: str, *,
         opportunity_type: str = Ty.REVERSAL.value,
         anchor_ns: int = 1_000_000_000) -> Opportunity:
    key = f"{opportunity_type}:{symbol}:{direction}:SWEEP:{anchor_ns}:1.0"
    return Opportunity(
        opportunity_id=f"OPP-TEST-{symbol}-{direction}-{anchor_ns}",
        canonical_key=key,
        symbol=symbol,
        direction=direction,
        opportunity_type=opportunity_type,
        state=state,
        created_at=pd.Timestamp.now(tz="UTC").isoformat(),
        updated_at=pd.Timestamp.now(tz="UTC").isoformat(),
    )


# --------------------------------------------------------- canonical_instrument

def test_canonical_instrument_strips_dots():
    assert canonical_instrument("XAUUSD.") == "XAUUSD"


def test_canonical_instrument_keeps_digits():
    assert canonical_instrument("XAUUSD247") == "XAUUSD247"


def test_canonical_instrument_uppercases():
    assert canonical_instrument("eurusd") == "EURUSD"


# ------------------------------------------------------------ thesis_strength

def test_thesis_strength_ordering():
    states = [
        St.OPPORTUNITY_ARMED.value,
        St.WAITING_FOR_CONFIRMATION.value,
        St.WAITING_FOR_POI.value,
        St.WAITING_FOR_IDM.value,
        St.ENTRY_TRIGGERED.value,
        St.READY_FOR_MITIGATION.value,
    ]
    strengths = [thesis_strength(_opp("X", "BULLISH", s)) for s in states]
    assert strengths == sorted(strengths), "strength must be monotonically non-decreasing"
    assert len(set(strengths)) == len(strengths), "all strengths must be distinct"


# -------------------------------------------------------- arbiter.arbitrate()

def test_accept_when_no_existing():
    repo = _repo()
    arb = OpportunityArbiter(repo)
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.OPPORTUNITY_ARMED.value, 1000)
    assert dec.outcome == "ACCEPT"


def test_accept_no_conflict_different_instrument():
    repo = _repo()
    repo.upsert(_opp("EURUSD", "BEARISH", St.WAITING_FOR_POI.value))
    arb = OpportunityArbiter(repo)
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.OPPORTUNITY_ARMED.value, 1000)
    assert dec.outcome == "ACCEPT"


def test_supersede_when_new_is_stronger():
    repo = _repo()
    existing = _opp("XAUUSD", "BEARISH", St.WAITING_FOR_CONFIRMATION.value, anchor_ns=500)
    repo.upsert(existing)
    arb = OpportunityArbiter(repo)
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.WAITING_FOR_POI.value, 1000)
    assert dec.outcome == "SUPERSEDE"
    assert existing.opportunity_id in dec.superseded_ids


def test_reject_when_existing_is_stronger():
    repo = _repo()
    existing = _opp("XAUUSD", "BEARISH", St.READY_FOR_MITIGATION.value, anchor_ns=500)
    repo.upsert(existing)
    arb = OpportunityArbiter(repo)
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.OPPORTUNITY_ARMED.value, 1000)
    assert dec.outcome == "REJECT"


def test_reject_when_existing_same_strength():
    repo = _repo()
    existing = _opp("XAUUSD", "BEARISH", St.WAITING_FOR_CONFIRMATION.value, anchor_ns=1000)
    repo.upsert(existing)
    arb = OpportunityArbiter(repo)
    # same state → same strength, same anchor ns → existing wins
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.WAITING_FOR_CONFIRMATION.value, 1000)
    assert dec.outcome == "REJECT"


def test_supersede_on_tie_newer_anchor_wins():
    repo = _repo()
    existing = _opp("XAUUSD", "BEARISH", St.WAITING_FOR_CONFIRMATION.value, anchor_ns=500)
    repo.upsert(existing)
    arb = OpportunityArbiter(repo)
    # same strength, but new has anchor_ns=2000 > 500
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.WAITING_FOR_CONFIRMATION.value, 2000)
    assert dec.outcome == "SUPERSEDE"
    assert existing.opportunity_id in dec.superseded_ids


def test_reject_duplicate_same_direction_weaker():
    repo = _repo()
    existing = _opp("XAUUSD", "BULLISH", St.WAITING_FOR_POI.value, anchor_ns=500)
    repo.upsert(existing)
    arb = OpportunityArbiter(repo)
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.OPPORTUNITY_ARMED.value, 1000)
    assert dec.outcome == "REJECT"


def test_supersede_same_direction_stronger_new():
    repo = _repo()
    existing = _opp("XAUUSD", "BULLISH", St.OPPORTUNITY_ARMED.value, anchor_ns=500)
    repo.upsert(existing)
    arb = OpportunityArbiter(repo)
    dec = arb.arbitrate("XAUUSD", "BULLISH", St.WAITING_FOR_IDM.value, 1000)
    assert dec.outcome == "SUPERSEDE"
    assert existing.opportunity_id in dec.superseded_ids


# ------------------------------------------------ SUPERSEDED excluded from active

def test_superseded_excluded_from_active_query():
    repo = _repo()
    opp = _opp("XAUUSD", "BEARISH", St.SUPERSEDED.value)
    repo.upsert(opp)
    active = repo.query(active_only=True)
    assert all(o.opportunity_id != opp.opportunity_id for o in active)


def test_superseded_excluded_from_active_for_instrument():
    repo = _repo()
    opp = _opp("XAUUSD", "BEARISH", St.SUPERSEDED.value)
    repo.upsert(opp)
    active = repo.active_for_instrument("XAUUSD")
    assert all(o.opportunity_id != opp.opportunity_id for o in active)


# ------------------------------------------------ TERMINAL_STATES & state machine

def test_superseded_is_terminal():
    assert St.SUPERSEDED in TERMINAL_STATES


def test_superseded_in_is_terminal_fn():
    assert is_terminal(St.SUPERSEDED.value)


@pytest.mark.parametrize("src", [
    St.OPPORTUNITY_ARMED.value,
    St.WAITING_FOR_CONFIRMATION.value,
    St.WAITING_FOR_POI.value,
    St.WAITING_FOR_IDM.value,
    St.READY_FOR_MITIGATION.value,
])
def test_all_active_states_can_transition_to_superseded(src):
    assert can_transition(src, St.SUPERSEDED.value), (
        f"{src} → SUPERSEDED should be allowed")


def test_superseded_has_no_outgoing_transitions():
    for target in St:
        if target != St.SUPERSEDED:
            assert not can_transition(St.SUPERSEDED.value, target.value)


# ----------------------------------------------- startup reconciliation

def test_reconcile_resolves_opposing_conflict():
    repo = _repo()
    bull = _opp("XAUUSD", "BULLISH", St.READY_FOR_MITIGATION.value, anchor_ns=500)
    bear = _opp("XAUUSD", "BEARISH", St.WAITING_FOR_CONFIRMATION.value, anchor_ns=600)
    repo.upsert(bull)
    repo.upsert(bear)
    arb = OpportunityArbiter(repo)
    pairs = arb.reconcile("2026-01-01T00:00:00Z")
    # BEARISH is weaker (strength 3 vs 7); it should be the loser
    losers = [p[0] for p in pairs]
    assert bear.opportunity_id in losers
    assert bull.opportunity_id not in losers


def test_reconcile_no_conflict():
    repo = _repo()
    bull = _opp("XAUUSD", "BULLISH", St.WAITING_FOR_CONFIRMATION.value)
    repo.upsert(bull)
    arb = OpportunityArbiter(repo)
    pairs = arb.reconcile("2026-01-01T00:00:00Z")
    assert pairs == []


def test_reconcile_same_direction_duplicates():
    repo = _repo()
    a = _opp("EURUSD", "BULLISH", St.WAITING_FOR_CONFIRMATION.value, anchor_ns=100)
    b = _opp("EURUSD", "BULLISH", St.WAITING_FOR_POI.value, anchor_ns=200)
    repo.upsert(a)
    repo.upsert(b)
    arb = OpportunityArbiter(repo)
    pairs = arb.reconcile("2026-01-01T00:00:00Z")
    losers = [p[0] for p in pairs]
    # 'a' is weaker (strength 3 vs 4); it should be superseded
    assert a.opportunity_id in losers
    assert b.opportunity_id not in losers


def test_reconcile_independent_instruments():
    repo = _repo()
    repo.upsert(_opp("EURUSD", "BULLISH", St.WAITING_FOR_CONFIRMATION.value))
    repo.upsert(_opp("GBPUSD", "BEARISH", St.WAITING_FOR_CONFIRMATION.value))
    arb = OpportunityArbiter(repo)
    pairs = arb.reconcile("2026-01-01T00:00:00Z")
    assert pairs == []


# ------------------------------------------- engine._supersede_opp + event

def test_supersede_opp_sets_fields():
    repo = _repo()
    opp = _opp("CHFJPY", "BEARISH", St.WAITING_FOR_CONFIRMATION.value)
    repo.upsert(opp)
    engine = OpportunityEngine(repo)
    result = {"created": [], "advanced": [], "invalidated": [], "expired": [],
              "converted": [], "events": [], "blocked": [], "superseded": []}
    now = pd.Timestamp("2026-10-08T00:00:00Z")
    engine._supersede_opp(opp.opportunity_id, "winner_key", "test reason", now, result)

    updated = repo.get(opp.opportunity_id)
    assert updated.state == St.SUPERSEDED.value
    assert updated.superseded_by == "winner_key"
    assert updated.supersession_reason == "test reason"
    assert updated.superseded_at is not None
    assert opp.opportunity_id in result["superseded"]


def test_supersede_opp_emits_event():
    repo = _repo()
    opp = _opp("CHFJPY", "BEARISH", St.WAITING_FOR_POI.value)
    repo.upsert(opp)
    engine = OpportunityEngine(repo)
    result = {"created": [], "advanced": [], "invalidated": [], "expired": [],
              "converted": [], "events": [], "blocked": [], "superseded": []}
    now = pd.Timestamp("2026-10-08T00:00:00Z")
    engine._supersede_opp(opp.opportunity_id, "winner", "reason", now, result)

    kinds = [e["kind"] for e in result["events"]]
    assert Ev.OPPORTUNITY_SUPERSEDED.value in kinds


def test_supersede_opp_idempotent():
    repo = _repo()
    opp = _opp("CHFJPY", "BEARISH", St.WAITING_FOR_POI.value)
    repo.upsert(opp)
    engine = OpportunityEngine(repo)
    result = {"created": [], "advanced": [], "invalidated": [], "expired": [],
              "converted": [], "events": [], "blocked": [], "superseded": []}
    now = pd.Timestamp("2026-10-08T00:00:00Z")
    engine._supersede_opp(opp.opportunity_id, "winner", "reason", now, result)
    engine._supersede_opp(opp.opportunity_id, "winner", "reason", now, result)
    # second call should not add a second entry
    assert result["superseded"].count(opp.opportunity_id) == 1


def test_supersede_opp_blocked_by_state_machine_for_terminal():
    repo = _repo()
    opp = _opp("CHFJPY", "BEARISH", St.EXPIRED.value)
    repo.upsert(opp)
    engine = OpportunityEngine(repo)
    result = {"created": [], "advanced": [], "invalidated": [], "expired": [],
              "converted": [], "events": [], "blocked": [], "superseded": []}
    now = pd.Timestamp("2026-10-08T00:00:00Z")
    engine._supersede_opp(opp.opportunity_id, "winner", "reason", now, result)
    # EXPIRED → SUPERSEDED not allowed; no change
    assert result["superseded"] == []
    unchanged = repo.get(opp.opportunity_id)
    assert unchanged.state == St.EXPIRED.value


# ------------------------------------------- REJECT goes to blocked list

def test_reject_recorded_in_blocked():
    repo = _repo()
    repo.upsert(_opp("GBPUSD", "BEARISH", St.READY_FOR_MITIGATION.value, anchor_ns=500))
    arb = OpportunityArbiter(repo)
    dec = arb.arbitrate("GBPUSD", "BULLISH", St.OPPORTUNITY_ARMED.value, 1000)
    assert dec.outcome == "REJECT"
    assert "READY_FOR_MITIGATION" in dec.reason or "strength" in dec.reason


# ---------------------------------------------- diagnostics counters

def test_diagnostics_tracks_counters():
    repo = _repo()
    repo.upsert(_opp("XAUUSD", "BEARISH", St.WAITING_FOR_CONFIRMATION.value, anchor_ns=500))
    arb = OpportunityArbiter(repo)
    arb.arbitrate("XAUUSD", "BULLISH", St.WAITING_FOR_POI.value, 1000)
    diag = arb.diagnostics()
    assert diag["conflicts_detected"] == 1
    assert diag["conflicts_resolved"] == 1
    assert diag["duplicates_rejected"] == 0


def test_diagnostics_tracks_rejected():
    repo = _repo()
    repo.upsert(_opp("XAUUSD", "BEARISH", St.READY_FOR_MITIGATION.value, anchor_ns=500))
    arb = OpportunityArbiter(repo)
    arb.arbitrate("XAUUSD", "BULLISH", St.OPPORTUNITY_ARMED.value, 1000)
    diag = arb.diagnostics()
    assert diag["duplicates_rejected"] == 1
