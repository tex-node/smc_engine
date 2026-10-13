"""Step 2 Slice 1 — sweep-of-IDM (Varis CONTINUATION precondition).

A CONTINUATION opportunity may only reach READY when an M15 sweep took out an
M15 IDM level (internal liquidity) after the arming BOS and at/before the
promoting POI. Synthetic fixtures only; zero broker interaction.

Regression guard: the primitive must read ``view.m15_sweeps`` (M15), never
``view.sweeps`` (H4). Matching an H4 liquidity pool to an M15 IDM level was the
null operation Step 0 found.
"""
from __future__ import annotations

import pandas as pd

from src.smc_engine.models import (Direction, Inducement, LiquiditySide,
                                   LiquiditySweep, StructureEvent,
                                   StructureEventType)
from src.smc_engine.opportunity import evaluator
from src.smc_engine.opportunity.engine import OpportunityEngine
from src.smc_engine.opportunity.evaluator import CausalView
from src.smc_engine.opportunity.funnel import FunnelReason, FunnelStage
from src.smc_engine.opportunity.models import (BlockReason, Opportunity,
                                               OpportunityState as St,
                                               OpportunityType as Ty,
                                               canonical_opportunity_key,
                                               opportunity_id_from_key)
from src.smc_engine.opportunity.repository import OpportunityRepository

BOS = pd.Timestamp("2026-10-12 12:00", tz="UTC")
POI = pd.Timestamp("2026-10-12 16:00", tz="UTC")
NOW = pd.Timestamp("2026-10-12 17:00", tz="UTC")
IDM_LEVEL = 1.10000
SWEEP_TIME = BOS + pd.Timedelta(hours=1)          # after BOS, before POI


# ── shared synthetic builders ────────────────────────────────────────────────

def _sweep(candle_time, side=LiquiditySide.SELL_SIDE, level=IDM_LEVEL,
           sid="SW-M15-1"):
    return LiquiditySweep(sid, side, level, level + 0.0005, f"LQ-{sid}", 10,
                          candle_time, level + 0.0002)


def _idm(direction=Direction.BULLISH, level=IDM_LEVEL):
    return Inducement("IDM-M15-1", direction, 10, BOS, level, 8,
                      "OB-M15-1-BULLISH")


def _bos(direction=Direction.BULLISH, time=BOS):
    return StructureEvent("BOS-1", StructureEventType.BOS, direction, 1.105, 10, time)


def _view(direction=Direction.BULLISH, breaks=None, sweeps=(), m15_sweeps=(),
          idms=()):
    return CausalView(
        symbol="EURUSD", as_of=NOW,
        breaks=list(breaks) if breaks is not None else [_bos(direction)],
        sweeps=list(sweeps), m15_sweeps=list(m15_sweeps), idms=list(idms),
        m15_last_time=NOW, m15_atr=0.001)


def _opp(direction="BULLISH", otype=Ty.CONTINUATION.value,
         state=St.OPPORTUNITY_ARMED.value):
    anchor = "BOS" if otype == Ty.CONTINUATION.value else "SWEEP"
    key = canonical_opportunity_key("EURUSD", direction, otype, anchor, BOS, 1.105)
    return Opportunity(opportunity_id=opportunity_id_from_key(key), canonical_key=key,
                       symbol="EURUSD", direction=direction, opportunity_type=otype,
                       state=state, created_at=BOS.isoformat(), updated_at=BOS.isoformat(),
                       bos_time=BOS.isoformat(),
                       bos_evidence={"id": "BOS-1", "time": BOS.isoformat()},
                       poi_time=POI.isoformat(), selected_poi="OB-M15-1-BULLISH")


def _res():
    return {"created": [], "advanced": [], "invalidated": [], "expired": [],
            "converted": [], "events": [], "blocked": [], "superseded": []}


def _engine(tmp_path, funnel_repo=None):
    repo = OpportunityRepository(str(tmp_path / "o.db"))
    return OpportunityEngine(repo, funnel_repo=funnel_repo), repo


def _promote(eng, opp, view):
    # No flag: the gate activates from the opportunity's own CONTINUATION type.
    eng._to_ready(opp, view, NOW, _res(), "test")


# ── 1. valid sweep-of-IDM promotes ───────────────────────────────────────────

def test_valid_m15_sweep_of_idm_promotes_continuation(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH")
    repo.upsert(opp)
    view = _view(Direction.BULLISH,
                 m15_sweeps=[_sweep(SWEEP_TIME)],
                 idms=[_idm(Direction.BULLISH)])
    _promote(eng, opp, view)
    row = repo.get(opp.opportunity_id)
    assert row.state == St.READY_FOR_MITIGATION.value
    assert row.idm_sweep_time == SWEEP_TIME.isoformat()
    assert row.idm_sweep_reference == "SW-M15-1"
    assert row.blocker in ("", None)


# ── 2. missing sweep-of-IDM refuses ──────────────────────────────────────────

def test_continuation_without_sweep_of_idm_refused(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH")
    repo.upsert(opp)
    view = _view(Direction.BULLISH, m15_sweeps=[], idms=[_idm(Direction.BULLISH)])
    _promote(eng, opp, view)
    row = repo.get(opp.opportunity_id)
    assert row.state != St.READY_FOR_MITIGATION.value
    assert row.blocker == BlockReason.SWEEP_OF_IDM_MISSING.value
    assert row.idm_sweep_reference == ""
    assert row.idm_sweep_time is None


def test_refusal_is_not_terminal(tmp_path):
    """A blocked continuation stays active (non-READY), never INVALIDATED."""
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH")
    repo.upsert(opp)
    view = _view(Direction.BULLISH, m15_sweeps=[], idms=[_idm(Direction.BULLISH)])
    _promote(eng, opp, view)
    row = repo.get(opp.opportunity_id)
    assert row.state not in (St.INVALIDATED.value, St.EXPIRED.value)
    assert any(o.opportunity_id == opp.opportunity_id
               for o in repo.query(symbol="EURUSD", active_only=True))


# ── 3. causal ordering ───────────────────────────────────────────────────────

def test_sweep_before_arming_bos_does_not_count(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH")
    repo.upsert(opp)
    view = _view(Direction.BULLISH,
                 m15_sweeps=[_sweep(BOS - pd.Timedelta(minutes=30))],
                 idms=[_idm(Direction.BULLISH)])
    _promote(eng, opp, view)
    row = repo.get(opp.opportunity_id)
    assert row.state != St.READY_FOR_MITIGATION.value
    assert row.blocker == BlockReason.SWEEP_OF_IDM_MISSING.value


def test_sweep_after_poi_does_not_count(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH")
    repo.upsert(opp)
    view = _view(Direction.BULLISH,
                 m15_sweeps=[_sweep(POI + pd.Timedelta(minutes=30))],
                 idms=[_idm(Direction.BULLISH)])
    _promote(eng, opp, view)
    row = repo.get(opp.opportunity_id)
    assert row.state != St.READY_FOR_MITIGATION.value
    assert row.blocker == BlockReason.SWEEP_OF_IDM_MISSING.value


# ── 4. direction / side match ────────────────────────────────────────────────

def test_side_mismatch_refused(tmp_path):
    """A SELL_SIDE sweep cannot satisfy a BEARISH setup (needs BUY_SIDE)."""
    eng, repo = _engine(tmp_path)
    opp = _opp("BEARISH")
    repo.upsert(opp)
    view = _view(Direction.BEARISH,
                 m15_sweeps=[_sweep(SWEEP_TIME, side=LiquiditySide.SELL_SIDE)],
                 idms=[_idm(Direction.BEARISH)])
    _promote(eng, opp, view)
    row = repo.get(opp.opportunity_id)
    assert row.state != St.READY_FOR_MITIGATION.value
    assert row.blocker == BlockReason.SWEEP_OF_IDM_MISSING.value


def test_matching_buy_side_sweep_promotes_bearish(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp("BEARISH")
    repo.upsert(opp)
    view = _view(Direction.BEARISH,
                 m15_sweeps=[_sweep(SWEEP_TIME, side=LiquiditySide.BUY_SIDE)],
                 idms=[_idm(Direction.BEARISH)])
    _promote(eng, opp, view)
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value


# ── 5. regression: H4 sweep must NOT satisfy the precondition ────────────────

def test_h4_sweep_at_idm_level_is_not_used():
    """The primitive reads view.m15_sweeps; an H4 pool at the IDM level is inert."""
    h4_sweep = _sweep(SWEEP_TIME, sid="SW-H4-1")           # level matches the IDM
    idm = _idm(Direction.BULLISH)
    h4_only = CausalView(symbol="EURUSD", as_of=NOW,
                         sweeps=[h4_sweep], m15_sweeps=[], idms=[idm])
    assert evaluator.sweep_of_idm(h4_only, Direction.BULLISH, BOS,
                                  poi_time=POI) is None
    m15_source = CausalView(symbol="EURUSD", as_of=NOW,
                            sweeps=[], m15_sweeps=[h4_sweep], idms=[idm])
    assert evaluator.sweep_of_idm(m15_source, Direction.BULLISH, BOS,
                                  poi_time=POI) is not None


def test_h4_sweep_matching_idm_level_does_not_promote(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH")
    repo.upsert(opp)
    view = _view(Direction.BULLISH,
                 sweeps=[_sweep(SWEEP_TIME, sid="SW-H4-1")],   # H4 only
                 m15_sweeps=[],
                 idms=[_idm(Direction.BULLISH)])
    _promote(eng, opp, view)
    row = repo.get(opp.opportunity_id)
    assert row.state != St.READY_FOR_MITIGATION.value
    assert row.blocker == BlockReason.SWEEP_OF_IDM_MISSING.value


# ── 6. funnel evidence ───────────────────────────────────────────────────────

def test_refusal_emits_sweep_of_idm_funnel(tmp_path):
    seen = []

    class FakeFunnel:
        def persist_events(self, events):
            seen.extend(events)

    eng, repo = _engine(tmp_path, funnel_repo=FakeFunnel())
    opp = _opp("BULLISH")
    repo.upsert(opp)
    view = _view(Direction.BULLISH, m15_sweeps=[], idms=[_idm(Direction.BULLISH)])
    _promote(eng, opp, view)
    assert seen, "a refusal must emit a funnel event"
    assert seen[0].stage == FunnelStage.SWEEP_OF_IDM.value
    assert seen[0].reason == FunnelReason.SWEEP_OF_IDM_MISSING.value


# ── 7. REVERSAL path unaffected ──────────────────────────────────────────────

def test_continuation_is_gated_directly_with_no_flag(tmp_path):
    """The gate activates from the opportunity's OWN type: a direct _to_ready
    call on a CONTINUATION opportunity is gated with no caller flag."""
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH")                       # opportunity_type = CONTINUATION
    repo.upsert(opp)
    view = _view(Direction.BULLISH, m15_sweeps=[], idms=[_idm(Direction.BULLISH)])
    eng._to_ready(opp, view, NOW, _res(), "test")
    row = repo.get(opp.opportunity_id)
    assert row.state != St.READY_FOR_MITIGATION.value
    assert row.blocker == BlockReason.SWEEP_OF_IDM_MISSING.value


def test_reversal_path_is_not_gated_by_sweep_of_idm(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH", otype=Ty.REVERSAL.value)
    repo.upsert(opp)
    view = _view(Direction.BULLISH, m15_sweeps=[], idms=[])
    eng._to_ready(opp, view, NOW, _res(), "test")   # REVERSAL: bias gate only
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value


def test_unrecognised_stream_fails_closed(tmp_path):
    """A stream that is neither REVERSAL nor CONTINUATION is never promoted."""
    eng, repo = _engine(tmp_path)
    opp = _opp("BULLISH", otype=Ty.PULLBACK.value)
    repo.upsert(opp)
    view = _view(Direction.BULLISH, m15_sweeps=[], idms=[])
    eng._to_ready(opp, view, NOW, _res(), "test")
    row = repo.get(opp.opportunity_id)
    assert row.state != St.READY_FOR_MITIGATION.value
    assert row.blocker == BlockReason.STRUCTURAL_INVALIDATION.value


# ── 8. view exposes M15 sweeps ───────────────────────────────────────────────

def test_build_view_exposes_m15_sweeps():
    d1, h4, m15 = _timeline()
    view = evaluator.build_view("GBPUSD", d1, h4, m15, m15["time"].iloc[-1],
                                _cfg())
    assert hasattr(view, "m15_sweeps")
    assert isinstance(view.m15_sweeps, list)
    # M15 sweeps and H4 sweeps are independent sources.
    assert view.sweeps is not view.m15_sweeps


def _cfg():
    from src.smc_engine.strategy import MultiTimeframeConfig
    return MultiTimeframeConfig()


def _timeline():
    from tests.test_opportunity_engine import timeline
    return timeline()
