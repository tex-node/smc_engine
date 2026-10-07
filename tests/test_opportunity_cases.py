"""Opportunity state-machine edge cases (opposing CSD, expiry, POI consumption,
continuation stream) — TEST FIXTURES, zero broker interaction."""
import pandas as pd
import pytest

from src.smc_engine.models import (Direction, LiquiditySide, LiquiditySweep,
                                   StructureEvent, StructureEventType)
from src.smc_engine.opportunity import (EntryPathway, OpportunityState as St,
                                        OpportunityType as Ty, OpportunityWindows)
from src.smc_engine.opportunity import evaluator
from src.smc_engine.opportunity.evaluator import CausalView, poi_candidates
from tests.test_opportunity_engine import CFG, _engine, _view, timeline


def test_opposing_csd_invalidates(tmp_path):
    eng, repo = _engine(tmp_path)
    eng.observe("GBPUSD", _view())
    opp = [o for o in repo.query(symbol="GBPUSD")
           if o.opportunity_type == Ty.REVERSAL.value][0]
    sweep = LiquiditySweep("SWEEP-TEST", LiquiditySide.SELL_SIDE, 102.3, 101.5,
                           "LQ-1", 1, pd.Timestamp("2026-01-25 00:00", tz="UTC"), 102.4)
    opposing = StructureEvent("CSD-OPP", StructureEventType.CSD, Direction.BEARISH,
                              103.0, 2, pd.Timestamp("2026-01-25 12:00", tz="UTC"))
    base = _view()
    view = CausalView(symbol="GBPUSD", as_of=pd.Timestamp("2026-01-25 13:00", tz="UTC"),
                      m15_frame=base.m15_frame,
                      m15_last_time=pd.Timestamp("2026-01-25 13:00", tz="UTC"),
                      sweeps=[sweep], csd_by_sweep={sweep.id: opposing})
    opp.created_at = "2026-01-25 01:00:00+00:00"      # before the opposing CSD
    opp.sweep_evidence = {"id": sweep.id, "side": "SELL_SIDE", "swept_level": 102.3,
                          "sweep_extreme": 101.5, "time": "2026-01-25 00:00:00+00:00"}
    opp.csd_evidence = {}
    opp.state = St.OPPORTUNITY_ARMED.value
    repo.upsert(opp)
    eng.observe("GBPUSD", view)
    final = [o for o in repo.query(symbol="GBPUSD")
             if o.opportunity_type == Ty.REVERSAL.value][0]
    assert final.state == St.INVALIDATED.value
    assert final.reason == "OPPOSING_CSD"


def test_sweep_window_expires_without_csd(tmp_path):
    d1, h4, m15 = timeline()
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(sweep_to_csd_bars=24))
    armed_at = None
    for t in list(m15["time"].iloc[::8]):
        view = evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG)
        if view.sweeps and not view.csd_by_sweep:
            view.csd_by_sweep = {}
            eng.observe("GBPUSD", view)
            armed_at = t
            break
    if armed_at is None:
        pytest.skip("fixture did not expose a pre-CSD sweep window")
    # a much later observation with no CSD in view must expire the armed sweep
    later = evaluator.build_view("GBPUSD", d1, h4, m15,
                                 pd.Timestamp("2026-01-27 00:00", tz="UTC"), CFG)
    later.csd_by_sweep = {}
    eng.observe("GBPUSD", later)
    opp = repo.query(symbol="GBPUSD")[0]
    assert opp.state == St.EXPIRED.value
    assert opp.reason in ("SWEEP_EXPIRED", "EXPIRED_TTL")


def test_poi_expiry_rejects_candidates_with_reason():
    from src.smc_engine.fvg import FairValueGap
    gap = FairValueGap(id="FVG-B-9", direction=Direction.BULLISH, top=105.0, bottom=104.5,
                       created_index=1, created_time=pd.Timestamp("2026-01-25 00:00", tz="UTC"),
                       state="ACTIVE")
    view = CausalView(symbol="T", as_of=pd.Timestamp("2026-01-27 00:00", tz="UTC"),
                      m15_last_time=pd.Timestamp("2026-01-27 00:00", tz="UTC"),
                      m15_fvgs=[gap], m15_atr=1.0)
    cands = poi_candidates(view, Direction.BULLISH, OpportunityWindows(poi_max_age_bars=4))
    assert len(cands) == 1 and cands[0].rejected_reason == "POI_EXPIRED"


def test_consumed_poi_rejected_and_reason_recorded():
    from src.smc_engine.fvg import FairValueGap
    gap = FairValueGap(id="FVG-B-1", direction=Direction.BULLISH, top=105.0, bottom=104.5,
                       created_index=1, created_time=pd.Timestamp("2026-01-25", tz="UTC"),
                       state="MITIGATED")
    view = CausalView(symbol="T", as_of=pd.Timestamp("2026-01-25 01:00", tz="UTC"),
                      m15_last_time=pd.Timestamp("2026-01-25 01:00", tz="UTC"),
                      m15_fvgs=[gap], m15_atr=1.0)
    cands = poi_candidates(view, Direction.BULLISH, OpportunityWindows())
    assert len(cands) == 1 and cands[0].rejected_reason == "POI_CONSUMED"


def test_continuation_stream_requires_post_bos_poi(tmp_path):
    """A BOS opens an independent continuation stream that waits for a new
    directionally valid POI formed AFTER the BOS."""
    base = _view()
    bos = StructureEvent("BOS-H-TEST", StructureEventType.BOS, Direction.BULLISH,
                         104.2, 10, pd.Timestamp("2026-01-26 12:00", tz="UTC"))
    view = CausalView(symbol="GBPUSD", as_of=base.as_of, m15_frame=base.m15_frame,
                      m15_last_time=base.m15_last_time, m15_atr=base.m15_atr,
                      breaks=[bos], m15_obs=base.m15_obs, m15_fvgs=base.m15_fvgs,
                      m15_swings=base.m15_swings, d1_pois=base.d1_pois)
    eng, repo = _engine(tmp_path)
    eng.observe("GBPUSD", view)
    conts = [o for o in repo.query(symbol="GBPUSD")
             if o.opportunity_type == Ty.CONTINUATION.value]
    assert conts, "BOS must open a continuation opportunity"
    c = conts[0]
    assert c.entry_pathway == EntryPathway.CONTINUATION.value
    assert pd.Timestamp(c.bos_evidence["time"]) == pd.Timestamp("2026-01-26 12:00", tz="UTC")
    assert c.state in (St.WAITING_FOR_POI.value, St.READY_FOR_MITIGATION.value,
                       St.ENTRY_TRIGGERED.value)
    if c.selected_poi:
        chosen = next(x for x in c.poi_candidates if x["poi_id"] == c.selected_poi)
        assert pd.Timestamp(chosen["created_time"]) >= pd.Timestamp(c.bos_evidence["time"])


def test_continuation_ttl_independent_of_reversal(tmp_path):
    """REVERSAL with a long TTL must survive a cycle that expires a
    CONTINUATION opportunity with a zero TTL."""
    base = _view()
    bos = StructureEvent("BOS-H-T2", StructureEventType.BOS, Direction.BULLISH,
                         104.2, 10, pd.Timestamp("2026-01-26 12:00", tz="UTC"))
    view = CausalView(symbol="GBPUSD", as_of=base.as_of, m15_frame=base.m15_frame,
                      m15_last_time=base.m15_last_time, m15_atr=base.m15_atr,
                      sweeps=base.sweeps, csd_by_sweep=base.csd_by_sweep,
                      breaks=[bos], m15_obs=base.m15_obs, m15_fvgs=base.m15_fvgs,
                      m15_swings=base.m15_swings, d1_pois=base.d1_pois,
                      idms=base.idms)
    eng, repo = _engine(tmp_path,
                        windows=OpportunityWindows(continuation_bos_to_poi_bars=0,
                                                   ready_ttl_bars=96,
                                                   sweep_to_csd_bars=96))
    eng.observe("GBPUSD", view)
    eng.expire_cycle(now=pd.Timestamp(base.as_of) + pd.Timedelta(minutes=1))
    revs = repo.query(symbol="GBPUSD", opportunity_type=Ty.REVERSAL.value, active_only=True)
    assert revs, "reversal opportunity must not be expired by the continuation TTL"
    assert not repo.query(symbol="GBPUSD", opportunity_type=Ty.CONTINUATION.value,
                          active_only=True), "continuation TTL must act independently"
