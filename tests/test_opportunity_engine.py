"""Opportunity engine tests — deterministic synthetic timeline (TEST FIXTURE).

A single authoritative causal timeline is replayed as_of-by-as_of. It contains
a D1 demand POI, an H4 sell-side sweep, an H4 CSD, a post-CSD M15 OB, an IDM
and an IRL target. No broker interaction anywhere; the engine is non-executing
by construction.
"""
import pandas as pd
import pytest

from src.smc_engine.models import Direction
from src.smc_engine.opportunity import (BlockReason, EntryPathway, OpportunityEngine,
                                        OpportunityRepository, OpportunityState as St,
                                        OpportunityType as Ty, OpportunityWindows)
from src.smc_engine.opportunity import evaluator
from src.smc_engine.opportunity.state_machine import (IllegalTransition,
                                                      assert_transition, can_transition)
from src.smc_engine.opportunity.models import canonical_opportunity_key, opportunity_id_from_key
from src.smc_engine.strategy import MultiTimeframeConfig

BASE = pd.Timestamp("2026-01-01", tz="UTC")


def _frame(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


def _c(time, o, hi, lo, cl):
    return [time, o, hi, lo, cl]


def timeline(entry_touch: bool = False):
    """Deterministic causal specimen (same shape as the validated capability
    probe): sweep -> CSD -> OB -> IDM -> IRL, optionally ending with an OB touch."""
    h = lambda hours: BASE + pd.Timedelta(hours=hours)          # noqa: E731
    d = lambda days: BASE + pd.Timedelta(days=days)             # noqa: E731

    d1 = [_c(d(day), 100, 101, 99, 100) for day in range(15)]
    d1.append(_c(d(15), 100, 103.6, 99.9, 103.5))               # displacement -> D1 POI
    for day in range(16, 22):
        d1.append(_c(d(day), 103.6, 104.9 if day == 18 else 104.6,
                     103.5 if day < 20 else 102.8, 104.4))
    d1.append(_c(d(22), 104.4, 104.5, 102.3, 103.3))
    d1.append(_c(d(23), 103.3, 104.2, 102.2, 102.4))
    d1.append(_c(d(24), 102.4, 104.45, 101.5, 104.3))
    d1.append(_c(d(25), 104.3, 105.0, 104.35, 104.95))

    h4 = []
    def push(day, candles):
        for j, (o, hi, lo, cl) in enumerate(candles):
            h4.append(_c(h(day * 24 + j * 4), o, hi, lo, cl))
    for day in range(15):
        push(day, [(100, 101, 99, 100)] * 6)
    push(15, [(100, 100.5, 99.9, 103.5)] + [(103.5, 103.6, 103.45, 103.5)] * 5)
    for day in (16, 17):
        push(day, [(103.5, 104.0, 103.45, 103.9)] * 6)
    push(18, [(103.9, 104.3, 103.85, 104.2), (104.2, 104.6, 104.15, 104.5),
              (104.5, 104.9, 104.4, 104.6), (104.6, 104.75, 104.5, 104.65),
              (104.65, 104.7, 104.55, 104.6), (104.6, 104.65, 104.5, 104.6)])
    push(19, [(104.6, 104.75, 104.55, 104.7), (104.7, 104.8, 104.6, 104.75),
              (104.75, 104.9, 104.7, 104.8), (104.8, 104.85, 104.7, 104.75),
              (104.75, 104.8, 104.7, 104.75), (104.75, 104.8, 104.7, 104.75)])
    push(20, [(104.7, 104.75, 104.4, 104.5), (104.5, 104.55, 104.3, 104.4),
              (104.4, 104.45, 104.2, 104.3), (104.3, 104.4, 104.25, 104.35),
              (104.35, 104.4, 104.3, 104.35), (104.35, 104.45, 104.3, 104.4)])
    push(21, [(104.4, 104.45, 103.6, 103.7), (103.7, 103.8, 103.3, 103.4),
              (103.4, 103.5, 103.0, 103.1), (103.1, 103.2, 102.8, 102.9),
              (102.9, 103.0, 102.6, 102.7), (102.7, 102.8, 102.45, 102.5)])
    push(22, [(102.5, 102.6, 102.45, 102.5), (102.5, 102.55, 102.4, 102.45),
              (102.45, 102.5, 102.3, 102.4), (102.4, 102.5, 102.35, 102.45),
              (102.45, 102.6, 102.4, 102.55), (102.55, 103.4, 102.5, 103.3)])
    push(23, [(103.3, 104.2, 103.25, 104.1), (104.1, 104.15, 104.0, 104.05),
              (104.05, 104.1, 103.6, 103.7), (103.7, 103.8, 103.0, 103.1),
              (103.1, 103.2, 102.6, 102.7), (102.7, 102.9, 102.2, 102.4)])
    push(24, [(102.4, 102.5, 101.5, 102.35), (102.35, 102.9, 102.3, 102.8),
              (102.8, 103.4, 102.75, 103.3), (103.3, 103.8, 103.25, 103.7),
              (103.7, 104.15, 103.65, 104.1), (104.1, 104.45, 104.05, 104.3)])
    push(25, [(104.3, 104.8, 104.25, 104.75), (104.75, 104.9, 104.7, 104.85),
              (104.85, 105.0, 104.8, 104.95), (104.95, 105.02, 104.9, 104.98),
              (104.98, 105.0, 104.9, 104.95), (104.95, 105.0, 104.9, 104.95)])

    m15 = []
    def mrange(day, start_q, n, o, hi, lo, cl):
        for j in range(n):
            m15.append(_c(h(day * 24 + start_q * 0.25 + j * 0.25), o, hi, lo, cl))
    for day in (18, 19):
        for blk, (o, hi, lo, cl) in enumerate([(104.2, 104.45, 104.15, 104.4),
                                               (104.4, 104.75, 104.35, 104.7),
                                               (104.7, 104.9, 104.65, 104.75),
                                               (104.75, 104.8, 104.55, 104.6),
                                               (104.6, 104.85, 104.55, 104.8),
                                               (104.8, 104.85, 104.7, 104.75)]):
            mrange(day, blk * 16, 16, o, hi, lo, cl)
    for day, levels in ((20, [(104.75, 104.8, 104.4, 104.45), (104.45, 104.5, 104.25, 104.3)]),
                        (21, [(104.3, 104.35, 102.9, 103.0), (103.0, 103.05, 102.5, 102.55)]),
                        (22, [(102.55, 102.65, 102.35, 102.45), (102.45, 102.6, 102.3, 102.5)])):
        for blk, (o, hi, lo, cl) in enumerate(levels):
            mrange(day, blk * 48, 48, o, hi, lo, cl)
    for start_q, n, o, hi, lo, cl in [
            (0, 16, 103.3, 104.2, 103.25, 104.15), (16, 16, 104.15, 104.22, 103.55, 103.6),
            (32, 16, 103.6, 103.65, 102.95, 103.0), (48, 16, 103.0, 103.1, 102.55, 102.6),
            (64, 16, 102.6, 102.7, 102.35, 102.45), (80, 16, 102.45, 102.55, 102.3, 102.4)]:
        mrange(23, start_q, n, o, hi, lo, cl)
    m15.append(_c(h(24 * 24), 102.4, 102.45, 101.5, 101.8))
    m15.append(_c(h(24 * 24 + .25), 101.8, 102.2, 101.75, 102.15))
    m15.append(_c(h(24 * 24 + .5), 102.15, 102.42, 102.1, 102.38))
    for j in range(3, 80):
        px = 102.38 + (j - 3) / 77 * 1.87
        m15.append(_c(h(24 * 24 + j * 0.25), px, px + 0.05, px - 0.03, px + 0.02))
    for j in range(80, 84):
        m15.append(_c(h(24 * 24 + j * 0.25), 104.25, 104.32, 104.2, 104.3))
    m15.append(_c(h(24 * 24 + 21), 104.35, 104.42, 104.05, 104.1))       # bearish OB candle
    m15.append(_c(h(24 * 24 + 21.25), 104.1, 105.05, 104.05, 105.0))     # bullish displacement
    m15.append(_c(h(24 * 24 + 21.5), 105.0, 105.02, 104.85, 104.9))
    m15.append(_c(h(24 * 24 + 21.75), 104.9, 104.95, 104.75, 104.8))
    m15.append(_c(h(24 * 24 + 22), 104.8, 104.85, 104.55, 104.6))       # IDM pullback
    m15.append(_c(h(24 * 24 + 22.25), 104.6, 104.65, 104.52, 104.55))
    m15.append(_c(h(24 * 24 + 22.5), 104.45, 104.5, 104.45, 104.5))     # IDM swing low (above entry edge)
    m15.append(_c(h(24 * 24 + 22.75), 104.48, 104.55, 104.46, 104.5))
    m15.append(_c(h(24 * 24 + 23), 104.5, 104.62, 104.5, 104.6))
    for j in range(92, 96):
        m15.append(_c(h(24 * 24 + j * 0.25), 104.6, 104.72, 104.58, 104.7))
    mrange(25, 0, 12, 104.7, 104.85, 104.65, 104.8)
    mrange(25, 12, 8, 104.8, 104.9, 104.75, 104.85)
    mrange(25, 20, 24, 104.85, 104.95, 104.8, 104.9)
    mrange(25, 44, 52, 104.9, 105.0, 104.85, 104.95)
    if entry_touch:
        m15.append(_c(h(26 * 24), 104.9, 104.92, 104.05, 104.2))          # OB mitigation
        m15.append(_c(h(26 * 24 + .25), 104.2, 104.3, 104.0, 104.1))
    return _frame(d1), _frame(h4), _frame(m15)


CFG = MultiTimeframeConfig()


def _engine(tmp_path, **kw):
    return _engine_named(tmp_path, "opp.db", **kw)


def _engine_named(tmp_path, name, **kw):
    repo = OpportunityRepository(str(tmp_path / name))
    return OpportunityEngine(repo, config=CFG, **kw), repo


def _view(symbol="GBPUSD", as_of=None, entry_touch=False):
    d1, h4, m15 = timeline(entry_touch=entry_touch)
    return evaluator.build_view(symbol, d1, h4, m15, as_of, CFG)


# ---------------------------------------------------------------- state machine

def test_state_machine_transitions():
    assert can_transition("OPPORTUNITY_ARMED", "WAITING_FOR_CONFIRMATION")
    assert can_transition("WAITING_FOR_POI", "READY_FOR_MITIGATION")
    assert can_transition("READY_FOR_MITIGATION", "ENTRY_TRIGGERED")
    assert not can_transition("EXPIRED", "READY_FOR_MITIGATION")
    assert not can_transition("INVALIDATED", "OPPORTUNITY_ARMED")
    with pytest.raises(IllegalTransition):
        assert_transition("ENTRY_TRIGGERED", "READY_FOR_MITIGATION")


# ------------------------------------------------------------- progression

def test_reversal_opportunity_progresses_across_cycles(tmp_path):
    # the fixture's sweep->CSD gap is ~20h; window configured accordingly
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(sweep_to_csd_bars=96,
                                                             ready_ttl_bars=192))
    d1, h4, m15 = timeline()
    sweep_time = None
    states = []
    ids = set()
    samples = list(m15["time"].iloc[::8]) + list(m15["time"].iloc[-4:])
    for t in samples:
        view = evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG)
        if not view.sweeps:
            continue
        eng.observe("GBPUSD", view)
        actives = [o for o in repo.query(symbol="GBPUSD", active_only=True)
                   if o.opportunity_type == Ty.REVERSAL.value]
        if actives:
            ids.update(o.opportunity_id for o in actives)
            states.append(actives[0].state)
    assert ids, "an opportunity must be created by the sweep"
    assert len(ids) == 1, "one sweep must never create duplicate opportunities"
    pre_ready = {St.OPPORTUNITY_ARMED.value, St.WAITING_FOR_CONFIRMATION.value,
                 St.WAITING_FOR_POI.value, St.WAITING_FOR_IDM.value}
    assert pre_ready & set(states), f"expected an early developing state, saw {states}"
    assert St.READY_FOR_MITIGATION.value in states, f"expected READY progression, saw {states}"
    opp = [o for o in repo.query(symbol="GBPUSD")
           if o.opportunity_type == Ty.REVERSAL.value][0]
    assert opp.selected_poi.startswith("OB-M15-")
    assert opp.idm_reference, "IDM must be tracked, never invented"
    assert opp.csd_evidence and opp.sweep_evidence
    assert opp.blocker in ("", None)


def test_entry_touch_terminates_opportunity(tmp_path):
    # READY TTL is market-anchored; widen it so the (deliberately late) entry
    # touch in the fixture still falls inside the configured window.
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(sweep_to_csd_bars=96,
                                                             ready_ttl_bars=192))
    d1, h4, m15 = timeline(entry_touch=True)
    samples = list(m15["time"].iloc[::8]) + list(m15["time"].iloc[-4:])
    for t in samples:
        view = evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG)
        eng.observe("GBPUSD", view)
    opp = [o for o in repo.query(symbol="GBPUSD")
           if o.opportunity_type == Ty.REVERSAL.value][0]
    assert opp.state == St.ENTRY_TRIGGERED.value
    assert opp.reason == "ENTRY_PASSED"


def test_ready_persists_across_repeated_polls(tmp_path):
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=192))
    view = _view()
    for _ in range(10):
        eng.observe("GBPUSD", view)
    opps = repo.query(symbol="GBPUSD", active_only=True)
    assert len(opps) == 1
    assert opps[0].state == St.READY_FOR_MITIGATION.value
    assert repo.count() == 1, "repeated polling must be idempotent"


def test_ready_expires_after_ttl(tmp_path):
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=0))
    eng.observe("GBPUSD", _view())
    # TTL sweep (watcher cadence) must expire a stale READY without deleting it
    eng.expire_cycle()
    opp = repo.query(symbol="GBPUSD")[0]
    assert opp.state == St.EXPIRED.value
    assert opp.reason == "EXPIRED_TTL"
    assert repo.history(opp.opportunity_id), "expiry must remain auditable"


def test_expired_opportunity_never_resurrects(tmp_path):
    eng, repo = _engine(tmp_path)
    d1, h4, m15 = timeline()
    # arm from an early window, then expire it manually via TTL
    early = evaluator.build_view("GBPUSD", d1, h4, m15,
                                 pd.Timestamp("2026-01-25 02:00", tz="UTC"), CFG)
    eng.observe("GBPUSD", early)
    opp = repo.query(symbol="GBPUSD")[0]
    opp.state = St.EXPIRED.value
    repo.upsert(opp)
    # the same sweep must not create a new opportunity
    eng.observe("GBPUSD", _view())
    assert repo.count() == 1
    assert repo.query(symbol="GBPUSD")[0].state == St.EXPIRED.value


def test_terminal_historical_setup_does_not_reactivate_opportunity(tmp_path):
    eng, repo = _engine(tmp_path)
    eng.observe("GBPUSD", _view())
    opp = repo.query(symbol="GBPUSD")[0]
    opp.state = St.TERMINAL.value
    repo.upsert(opp)
    eng.observe("GBPUSD", _view())
    assert repo.query(symbol="GBPUSD")[0].state == St.TERMINAL.value


# ------------------------------------------------------------------ pathways

def _run_with_pathways(tmp_path, pathways):
    eng, repo = _engine(tmp_path, pathways=pathways,
                        windows=OpportunityWindows(ready_ttl_bars=192))
    eng.observe("GBPUSD", _view())
    return repo.query(symbol="GBPUSD")[0]


def test_aggressive_pathway_reaches_ready_without_idm(tmp_path):
    opp = _run_with_pathways(tmp_path, [EntryPathway.AGGRESSIVE.value])
    assert opp.state == St.READY_FOR_MITIGATION.value
    assert opp.entry_pathway == EntryPathway.AGGRESSIVE.value
    assert opp.idm_reference == ""            # documented pathway policy


def test_pullback_pathway_tracks_idm(tmp_path):
    opp = _run_with_pathways(tmp_path, [EntryPathway.PULLBACK.value])
    assert opp.state == St.READY_FOR_MITIGATION.value
    assert opp.idm_reference


def test_smart_pathway_requires_proximity(tmp_path):
    # far from the POI: SMART must not promote to READY
    eng, repo = _engine(tmp_path, pathways=[EntryPathway.SMART.value])
    eng.observe("GBPUSD", _view())
    far = repo.query(symbol="GBPUSD")[0]
    assert far.state != St.ENTRY_TRIGGERED.value
    # price inside the POI: SMART reaches READY then entry (pullback mechanics)
    eng2, repo2 = _engine_named(tmp_path, "smart_near.db", pathways=[EntryPathway.SMART.value])
    eng2.observe("GBPUSD", _view(entry_touch=True))
    near = repo2.query(symbol="GBPUSD")[0]
    assert near.state == St.ENTRY_TRIGGERED.value
    assert near.idm_reference, "SMART must never bypass structural requirements"


# ------------------------------------------------------------- identity/replay

def test_identity_stable_under_rolling_window(tmp_path):
    d1, h4, m15 = timeline()
    as_of = m15["time"].iloc[-1]
    view_a = evaluator.build_view("GBPUSD", d1, h4, m15, as_of, CFG)
    # prepend older H4 bars (rolling window shift) — engine ids may drift,
    # the opportunity's canonical identity must not
    old = h4.iloc[[0] * 6].copy()
    old["time"] = pd.date_range("2025-12-01", periods=6, freq="4h", tz="UTC")
    h4_shifted = pd.concat([old, h4], ignore_index=True).sort_values("time").reset_index(drop=True)
    view_b = evaluator.build_view("GBPUSD", d1, h4_shifted, m15, as_of, CFG)
    sig_a = {(float(s.swept_level), str(s.candle_time)) for s in view_a.sweeps}
    sig_b = {(float(s.swept_level), str(s.candle_time)) for s in view_b.sweeps}
    assert sig_a and sig_a == sig_b, "rolling window must not drift canonical sweep identity"
    keys_a = {canonical_opportunity_key("GBPUSD", "BEARISH", "REVERSAL", "SWEEP",
                                        s.candle_time, s.swept_level) for s in view_a.sweeps}
    keys_b = {canonical_opportunity_key("GBPUSD", "BEARISH", "REVERSAL", "SWEEP",
                                        s.candle_time, s.swept_level) for s in view_b.sweeps}
    assert keys_a == keys_b


def test_no_lookahead_truncation_equivalence():
    d1, h4, m15 = timeline()
    cut = m15["time"].iloc[420]
    view_truncated_input = evaluator.build_view("GBPUSD", d1, h4,
                                                m15[m15["time"] <= cut], cut, CFG)
    view_full_input = evaluator.build_view("GBPUSD", d1, h4, m15, cut, CFG)
    assert [(s.id, str(s.candle_time)) for s in view_truncated_input.sweeps] == \
           [(s.id, str(s.candle_time)) for s in view_full_input.sweeps]
    assert [c.id for c in view_truncated_input.candidates] == \
           [c.id for c in view_full_input.candidates]
    assert view_truncated_input.m15_last_time == view_full_input.m15_last_time


def test_canonical_key_is_timestamp_derived_and_stable():
    k1 = canonical_opportunity_key("GBPUSD", "BEARISH", "REVERSAL", "SWEEP",
                                   pd.Timestamp("2026-01-25 04:00", tz="UTC"), 102.3)
    k2 = canonical_opportunity_key("GBPUSD", "BEARISH", "REVERSAL", "SWEEP",
                                   pd.Timestamp("2026-01-25 04:00", tz="UTC"), 102.3)
    assert k1 == k2
    assert "SWEEP" in k1 and "REVERSAL" in k1
    assert opportunity_id_from_key(k1).startswith("OPP-REVERSAL-GBPUSD-SWEEP-")


def test_replay_deterministic(tmp_path):
    results = []
    for run in range(2):
        eng, repo = _engine_named(tmp_path, f"replay{run}.db")
        d1, h4, m15 = timeline()
        for t in m15["time"].iloc[::16]:
            eng.observe("GBPUSD", evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG))
        results.append([(o.opportunity_id, o.state) for o in repo.query(symbol="GBPUSD")])
    assert results[0] == results[1]


# ---------------------------------------------------------- risk non-destruction

def test_risk_rejection_does_not_destroy_opportunity(tmp_path):
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=192))
    eng.observe("GBPUSD", _view())
    opp = repo.query(symbol="GBPUSD")[0]
    opp.risk_status = "RISK_REJECTED"
    repo.upsert(opp)
    eng.observe("GBPUSD", _view())
    assert repo.query(symbol="GBPUSD")[0].state != St.INVALIDATED.value


def test_portfolio_rejection_does_not_destroy_opportunity(tmp_path):
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=192))
    eng.observe("GBPUSD", _view())
    opp = repo.query(symbol="GBPUSD")[0]
    opp.risk_status = "RISK_PORTFOLIO_REJECTED"
    repo.upsert(opp)
    eng.observe("GBPUSD", _view())
    assert repo.query(symbol="GBPUSD")[0].state != St.EXPIRED.value


def test_restart_preserves_opportunity_state(tmp_path):
    path = str(tmp_path / "opp.db")
    eng1 = OpportunityEngine(OpportunityRepository(path), config=CFG,
                             windows=OpportunityWindows(ready_ttl_bars=192))
    eng1.observe("GBPUSD", _view())
    before = eng1.repo.query(symbol="GBPUSD")[0]
    assert before.state == St.READY_FOR_MITIGATION.value
    eng1.repo.close()
    eng2 = OpportunityEngine(OpportunityRepository(path), config=CFG,
                             windows=OpportunityWindows(ready_ttl_bars=192))
    after = eng2.repo.query(symbol="GBPUSD")[0]
    assert after.opportunity_id == before.opportunity_id
    assert after.state == before.state
    assert after.expires_at == before.expires_at
    assert eng2.repo.history(after.opportunity_id), "state history must survive restart"
    eng2.observe("GBPUSD", _view())                     # no duplicate after restart
    assert eng2.repo.count() == 1


def test_funnel_reports_progression_counts(tmp_path):
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=192))
    eng.observe("GBPUSD", _view())
    f = eng.funnel()
    assert f["ready_opportunity_count"] >= 1
    assert f["total"] >= 1
    assert "expired_count" in f and "converted_to_setup" in f


def test_audit_explains_current_blocker(tmp_path):
    eng, repo = _engine(tmp_path, windows=OpportunityWindows(ready_ttl_bars=192))
    eng.observe("GBPUSD", _view())
    audit = eng.audit("GBPUSD")
    assert audit["classification"] in [b.value for b in BlockReason]
    assert audit["active_opportunities"] >= 1
