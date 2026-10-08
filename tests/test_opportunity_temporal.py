"""P1-A temporal-invariant regressions: no causally invalid anchor->POI pairing.

Event-time based only; TEST FIXTURES; zero broker interaction.
"""
import pandas as pd
import pytest

from src.smc_engine.models import Direction
from src.smc_engine.opportunity import (BlockReason, Opportunity, OpportunityEngine,
                                        OpportunityRepository, OpportunityState as St,
                                        OpportunityType as Ty, OpportunityWindows)
from src.smc_engine.opportunity import evaluator
from src.smc_engine.opportunity.models import canonical_opportunity_key, opportunity_id_from_key
from tests.test_opportunity_engine import CFG, _engine, _view, timeline

W = OpportunityWindows()


def _persisted(tmp_path, name, sweep_time, csd_time=None, poi_time=None,
               otype=Ty.REVERSAL.value, state=St.READY_FOR_MITIGATION.value,
               selected_poi="OB-M15-1-BULLISH"):
    repo = OpportunityRepository(str(tmp_path / name))
    key = canonical_opportunity_key("GBPUSD", "BULLISH", otype,
                                    "SWEEP" if otype == Ty.REVERSAL.value else "BOS",
                                    sweep_time, 1.0)
    opp = Opportunity(opportunity_id=opportunity_id_from_key(key), canonical_key=key,
                      symbol="GBPUSD", direction="BULLISH", opportunity_type=otype,
                      state=state, created_at=str(sweep_time), updated_at=str(sweep_time),
                      sweep_time=str(sweep_time),
                      sweep_evidence={"id": "SW-X", "time": str(sweep_time)},
                      csd_time=str(csd_time) if csd_time else None,
                      csd_evidence={"id": "CSD-X", "time": str(csd_time)} if csd_time else {},
                      bos_time=str(sweep_time) if otype == Ty.CONTINUATION.value else None,
                      bos_evidence={"id": "BOS-X", "time": str(sweep_time)}
                      if otype == Ty.CONTINUATION.value else {},
                      poi_time=str(poi_time) if poi_time else None,
                      selected_poi=selected_poi if poi_time else "")
    repo.upsert(opp)
    eng = OpportunityEngine(repo, config=CFG)
    return eng, repo, opp


def test_old_csd_with_current_poi_is_stale_anchor(tmp_path):
    """The audit's exact defect: ancient CSD paired with a current POI."""
    sweep = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    csd = pd.Timestamp("2026-01-01 02:00", tz="UTC")          # inside sweep window
    poi = pd.Timestamp("2026-01-20 00:00", tz="UTC")          # 19 days after CSD
    eng, repo, opp = _persisted(tmp_path, "stale.db", sweep, csd, poi)
    view = _view()                                            # full market view
    eng.observe("GBPUSD", view)
    row = repo.get(opp.opportunity_id)
    assert row.state == St.INVALIDATED.value
    assert row.reason == BlockReason.STALE_ANCHOR.value
    detail = " ".join(h["detail"] or "" for h in repo.history(opp.opportunity_id))
    assert "CSD->POI" in detail or "sweep->CSD" in detail
    # never mislabelled as the other reasons
    assert row.reason not in ("POI_NOT_FOUND", "EXPIRED_TTL", "RISK_REJECTED")


def test_old_sweep_with_current_csd_is_not_armed(tmp_path):
    """sweep -> CSD beyond the configured window must never arm."""
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(sweep_to_csd_bars=4))
    d1, h4, m15 = timeline()
    view = evaluator.build_view("GBPUSD", d1, h4, m15, m15["time"].iloc[-1], CFG)
    assert view.sweeps, "fixture must expose a sweep for this test"
    eng.observe("GBPUSD", view)
    assert repo.count() == 0, "stale sweep->CSD chain must not create an opportunity"


def test_valid_same_session_chain_is_accepted(tmp_path):
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=192))
    eng.observe("GBPUSD", _view())
    row = [o for o in repo.query(symbol="GBPUSD")
           if o.opportunity_type == Ty.REVERSAL.value][0]
    assert row.state == St.READY_FOR_MITIGATION.value
    assert row.sweep_time and row.csd_time and row.poi_time
    assert pd.Timestamp(row.poi_time) - pd.Timestamp(row.csd_time) <= pd.Timedelta(hours=24)


def test_poi_just_inside_window_accepted_and_just_outside_rejected():
    from src.smc_engine.fvg import FairValueGap
    csd = pd.Timestamp("2026-01-10 00:00", tz="UTC")
    inside = csd + pd.Timedelta(minutes=15 * (W.csd_to_poi_bars - 1))
    outside = csd + pd.Timedelta(minutes=15 * (W.csd_to_poi_bars + 1))
    view = evaluator.CausalView(
        symbol="T", as_of=csd + pd.Timedelta(hours=1),
        m15_last_time=csd + pd.Timedelta(hours=1), m15_atr=1.0,
        m15_fvgs=[FairValueGap(id="FVG-B-in", direction=Direction.BULLISH,
                               top=105.0, bottom=104.0, created_index=1,
                               created_time=inside, state="ACTIVE")])
    cands = evaluator.poi_candidates(view, Direction.BULLISH, W,
                                     created_after=csd,
                                     created_before=csd + pd.Timedelta(minutes=15 * W.csd_to_poi_bars))
    assert cands[0].rejected_reason is None, "POI just inside the window must be accepted"

    view2 = evaluator.CausalView(
        symbol="T", as_of=outside + pd.Timedelta(hours=1),
        m15_last_time=outside + pd.Timedelta(hours=1), m15_atr=1.0,
        m15_fvgs=[FairValueGap(id="FVG-B-out", direction=Direction.BULLISH,
                               top=105.0, bottom=104.0, created_index=1,
                               created_time=outside, state="ACTIVE")])
    cands2 = evaluator.poi_candidates(view2, Direction.BULLISH, W,
                                      created_after=csd,
                                      created_before=csd + pd.Timedelta(minutes=15 * W.csd_to_poi_bars))
    assert cands2[0].rejected_reason == BlockReason.STALE_ANCHOR.value


def test_repeated_polling_does_not_refresh_ttl(tmp_path):
    """Successive polling cycles (advancing market time) must advance the
    observation metadata but never rejuvenate the READY expiry."""
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=192))
    d1, h4, m15 = timeline()
    samples = list(m15["time"].iloc[::8]) + list(m15["time"].iloc[-4:])
    seen = []
    for t in samples:
        eng.observe("GBPUSD", evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG))
        rows = [o for o in repo.query(symbol="GBPUSD")
                if o.opportunity_type == Ty.REVERSAL.value]
        if rows and rows[0].state == St.READY_FOR_MITIGATION.value:
            seen.append((rows[0].expires_at, rows[0].observed_time))
    assert len(seen) >= 2, "fixture must reach and hold READY across cycles"
    expiries = {e for e, _ in seen}
    observations = {o for _, o in seen}
    assert len(expiries) == 1, f"READY TTL must not be refreshed by polling: {expiries}"
    assert len(observations) >= 2, "observation time must advance across cycles"


def test_restart_does_not_refresh_ttl(tmp_path):
    path = str(tmp_path / "rt.db")
    eng1 = OpportunityEngine(OpportunityRepository(path), config=CFG,
                             windows=OpportunityWindows(ready_ttl_bars=192))
    eng1.observe("GBPUSD", _view())
    before = [o for o in eng1.repo.query(symbol="GBPUSD")
              if o.opportunity_type == Ty.REVERSAL.value][0]
    eng1.repo.close()
    eng2 = OpportunityEngine(OpportunityRepository(path), config=CFG,
                             windows=OpportunityWindows(ready_ttl_bars=192))
    eng2.observe("GBPUSD", _view())
    after = [o for o in eng2.repo.query(symbol="GBPUSD")
             if o.opportunity_type == Ty.REVERSAL.value][0]
    assert after.expires_at == before.expires_at
    assert after.state == before.state


def test_temporal_validation_uses_event_time_not_observation_time(tmp_path):
    """A chain that is temporally valid at the event times must not become
    STALE merely because it is observed later (within the READY TTL)."""
    eng, repo, opp = _persisted(
        tmp_path, "evt.db",
        sweep_time=pd.Timestamp("2026-01-24 20:00", tz="UTC"),
        csd_time=pd.Timestamp("2026-01-25 20:00", tz="UTC"),
        poi_time=pd.Timestamp("2026-01-25 21:00", tz="UTC"))
    eng.observe("GBPUSD", _view())              # observed much later
    row = repo.get(opp.opportunity_id)
    assert row.state != St.INVALIDATED.value, "valid chain must not be stale-terminated"


def test_legacy_payload_without_poi_time_is_still_audited(tmp_path):
    """Pre-fix rows carry selected_poi + poi_candidates but a NULL poi_time.

    The causal invariant must still be evaluated from the selected candidate's
    created_time, otherwise a stale pairing survives termination forever and
    keeps showing as READY / entry-eligible.
    """
    repo = OpportunityRepository(str(tmp_path / "legacy.db"))
    sweep = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    csd = pd.Timestamp("2026-01-01 02:00", tz="UTC")
    stale_poi = pd.Timestamp("2026-01-20 00:00", tz="UTC")     # 19 days after CSD
    key = canonical_opportunity_key("GBPUSD", "BULLISH", Ty.REVERSAL.value,
                                    "SWEEP", sweep, 1.0)
    opp = Opportunity(
        opportunity_id=opportunity_id_from_key(key), canonical_key=key,
        symbol="GBPUSD", direction="BULLISH",
        opportunity_type=Ty.REVERSAL.value, state=St.READY_FOR_MITIGATION.value,
        created_at=str(sweep), updated_at=str(sweep),
        sweep_time=str(sweep),
        sweep_evidence={"id": "SW-X", "time": str(sweep)},
        csd_evidence={"id": "CSD-X", "time": str(csd)},
        # legacy shape: market-event timestamp fields are NULL
        csd_time=None, poi_time=None, selected_poi="OB-M15-LEGACY-BULLISH",
        poi_candidates=[{"poi_id": "OB-M15-LEGACY-BULLISH", "kind": "OB",
                         "direction": "BULLISH", "low": 1.0, "high": 1.1,
                         "created_time": str(stale_poi), "mitigated": False,
                         "rejected_reason": None}])
    repo.upsert(opp)
    eng = OpportunityEngine(repo, config=CFG)
    eng.observe("GBPUSD", _view())
    row = repo.get(opp.opportunity_id)
    assert row.state == St.INVALIDATED.value
    assert row.reason == BlockReason.STALE_ANCHOR.value
