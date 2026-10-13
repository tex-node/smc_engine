"""Phase B — HTF-bias gate (Varis SMC). Synthetic fixtures only; disposable DBs."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from src.smc_engine.models import Direction, StructureEvent, StructureEventType
from src.smc_engine.opportunity.engine import OpportunityEngine
from src.smc_engine.opportunity.evaluator import htf_bias
from src.smc_engine.opportunity.models import (Opportunity, OpportunityState as St,
                                               OpportunityType as Ty,
                                               canonical_opportunity_key,
                                               opportunity_id_from_key)
from src.smc_engine.opportunity.repository import OpportunityRepository

NOW = pd.Timestamp("2026-10-12 16:00", tz="UTC")


def _bos(direction, time="2026-10-12 12:00"):
    return StructureEvent(id="BOS-X", type=StructureEventType.BOS, direction=direction,
                          level=1.1, candle_index=10, candle_time=pd.Timestamp(time, tz="UTC"))


def _poi(direction, time="2026-10-11 00:00"):
    return SimpleNamespace(direction=direction, created_time=pd.Timestamp(time, tz="UTC"))


def _view(breaks=(), d1_pois=()):
    return SimpleNamespace(breaks=list(breaks), d1_pois=list(d1_pois))


def _opp(direction="BEARISH", otype=Ty.REVERSAL.value, state=St.OPPORTUNITY_ARMED.value):
    # REVERSAL: these tests exercise the HTF-bias gate in isolation. A
    # CONTINUATION opportunity would additionally require a sweep of an M15 IDM
    # level (Step 2 Slice 1) — covered separately by tests/test_sweep_of_idm.py.
    anchor_kind = "BOS" if otype == Ty.CONTINUATION.value else "SWEEP"
    k = canonical_opportunity_key("EURUSD", direction, otype, anchor_kind,
                                  pd.Timestamp("2026-10-12 12:00", tz="UTC"), 1.1)
    return Opportunity(opportunity_id=opportunity_id_from_key(k), canonical_key=k,
                       symbol="EURUSD", direction=direction, opportunity_type=otype,
                       state=state, created_at="2026-10-12T12:00:00+00:00",
                       updated_at="2026-10-12T12:00:00+00:00")


def _res():
    return {"created": [], "advanced": [], "invalidated": [], "expired": [],
            "converted": [], "events": [], "blocked": [], "superseded": []}


def _engine(tmp_path):
    repo = OpportunityRepository(str(tmp_path / "o.db"))
    return OpportunityEngine(repo), repo


# ── B1: bias source ──────────────────────────────────────────────────────────

def test_bias_source_unknown_without_h4_bos():
    assert htf_bias(_view())["bias"] == "UNKNOWN"


def test_bias_source_agrees_with_h4_bos():
    b = htf_bias(_view(breaks=[_bos(Direction.BEARISH)]))
    assert b["h4_bos_direction"] == "BEARISH" and b["bias"] == "BEARISH"


def test_bias_source_unknown_on_d1_conflict():
    b = htf_bias(_view(breaks=[_bos(Direction.BEARISH)], d1_pois=[_poi(Direction.BULLISH)]))
    assert b["bias"] == "UNKNOWN"


def test_bias_source_d1_agreement_keeps_bias():
    b = htf_bias(_view(breaks=[_bos(Direction.BULLISH)], d1_pois=[_poi(Direction.BULLISH)]))
    assert b["bias"] == "BULLISH"


# ── B3: gate on promotion ────────────────────────────────────────────────────

def test_continuation_same_direction_promotes(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp(direction="BEARISH")
    repo.upsert(opp)
    res = _res()
    eng._to_ready(opp, _view(breaks=[_bos(Direction.BEARISH)]), NOW, res, "test")
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value


def test_continuation_opposite_direction_refused(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp(direction="BULLISH")
    repo.upsert(opp)
    res = _res()
    eng._to_ready(opp, _view(breaks=[_bos(Direction.BEARISH)]), NOW, res, "test")
    assert repo.get(opp.opportunity_id).state == St.INVALIDATED.value
    hist = repo.history(opp.opportunity_id)
    assert hist and hist[-1]["reason"] == "HTF_BIAS_MISMATCH"
    assert any(b.get("reason") == "htf_bias_mismatch" for b in res["blocked"])


def test_continuation_unknown_bias_refused(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp(direction="BEARISH")
    repo.upsert(opp)
    res = _res()
    eng._to_ready(opp, _view(), NOW, res, "test")
    assert repo.get(opp.opportunity_id).state == St.INVALIDATED.value
    assert repo.history(opp.opportunity_id)[-1]["reason"] == "NO_HTF_CONTEXT"


def test_reversal_same_gate_promotes(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp(direction="BEARISH", otype=Ty.REVERSAL.value)
    repo.upsert(opp)
    eng._to_ready(opp, _view(breaks=[_bos(Direction.BEARISH)]), NOW, _res(), "test")
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value


def test_reversal_same_gate_refuses(tmp_path):
    eng, repo = _engine(tmp_path)
    opp = _opp(direction="BULLISH", otype=Ty.REVERSAL.value)
    repo.upsert(opp)
    eng._to_ready(opp, _view(breaks=[_bos(Direction.BEARISH)]), NOW, _res(), "test")
    assert repo.get(opp.opportunity_id).state == St.INVALIDATED.value


# ── B4: funnel emission ──────────────────────────────────────────────────────

def test_refusal_emits_funnel_event(tmp_path):
    repo = OpportunityRepository(str(tmp_path / "o.db"))
    seen = []

    class FakeFunnel:
        def persist_events(self, events):
            seen.extend(events)

    eng = OpportunityEngine(repo, funnel_repo=FakeFunnel())
    opp = _opp(direction="BULLISH")
    repo.upsert(opp)
    eng._to_ready(opp, _view(breaks=[_bos(Direction.BEARISH)]), NOW, _res(), "test")
    assert seen and seen[0].reason == "HTF_BIAS_MISMATCH" and seen[0].stage == "HTF_CONTEXT"
