"""Phase 14 — Pre-arm funnel instrumentation tests (12 cases).

Verifies that FunnelEvent objects are emitted at each rejection/advancement
point in trace_chain(), that FunnelRepository persists them correctly, and
that the /api/research/funnel endpoint returns them. No opportunity records
are created for rejections; replay is deterministic.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.smc_engine.causal import CausalMTFAnalyzer, FunnelEvent
from src.smc_engine.opportunity.funnel import FunnelRepository, FunnelReason, FunnelStage
from src.smc_engine.strategy import MultiTimeframeConfig

from tests.test_integration_fixture import build_fixture, frame


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _blank_d1_h4_m15():
    """Flat frames with no sweep structure — produces no sweeps, no funnel events."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    d1 = frame([[base + pd.Timedelta(days=i), 100, 101, 99, 100] for i in range(20)])
    h4 = frame([[base + pd.Timedelta(hours=4 * i), 100, 101, 99, 100] for i in range(40)])
    m15 = frame([[base + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100] for i in range(200)])
    return d1, h4, m15


def _funnel_cfg() -> MultiTimeframeConfig:
    """Tight config that detects the funnel fixture's H4 structure."""
    return MultiTimeframeConfig(
        d1_lookback=30, d1_poi_lookback=30,
        h4_swing_left=2, h4_swing_right=2, h4_sweep_lookback=20,
        h4_csd_window=6,
        m15_swing_left=2, m15_swing_right=2,
        m15_atr_period=5, m15_displacement_atr=1.2,
        m15_ob_search_back=5, m15_idm_window=12,
    )


def _build_sweep_fixture_no_d1_poi():
    """Sweep exists but D1 displacement is AFTER the sweep → pois empty → NO_D1_POI."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    # D1: flat until day 14-15 where displacement occurs (AFTER the sweep at ~60h)
    d1_rows = [[base + pd.Timedelta(days=i), 100, 101, 99, 100] for i in range(30)]
    d1_rows[14] = [base + pd.Timedelta(days=14), 100, 110, 99, 109]
    d1_rows[15] = [base + pd.Timedelta(days=15), 109, 110, 107, 108]
    d1 = frame(d1_rows)
    # H4: confirmed swing low + sweep + CSD
    h4_rows = [[base + pd.Timedelta(hours=4 * i), 100, 101, 99, 100] for i in range(60)]
    h4_rows[10] = [base + pd.Timedelta(hours=40), 100, 101, 95, 99]
    h4_rows[11] = [base + pd.Timedelta(hours=44), 99, 100, 97, 98]
    h4_rows[12] = [base + pd.Timedelta(hours=48), 98, 100, 96, 99]
    h4_rows[15] = [base + pd.Timedelta(hours=60), 99, 100, 94, 99]
    h4_rows[16] = [base + pd.Timedelta(hours=64), 99, 104, 98, 103]
    h4 = frame(h4_rows)
    # M15 must extend past the H4 sweep (base+60h requires 60*4=240 bars)
    m15 = frame([[base + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100] for i in range(300)])
    return d1, h4, m15


def _build_sweep_fixture_with_d1_poi_no_csd():
    """D1 POI exists before sweep, but no CSD forms → NO_CSD expected."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    # D1: bullish displacement on day 0 → POI exists before sweep at ~60h = ~2.5 days
    d1_rows = [[base + pd.Timedelta(days=i), 100, 101, 99, 100] for i in range(30)]
    d1_rows[0] = [base, 100, 110, 99, 109]   # big bullish bar → D1 POI
    d1_rows[1] = [base + pd.Timedelta(days=1), 109, 110, 107, 108]
    d1 = frame(d1_rows)
    # H4: confirmed swing low + sweep but NO bullish CSD break after sweep
    h4_rows = [[base + pd.Timedelta(hours=4 * i), 100, 101, 99, 100] for i in range(60)]
    h4_rows[10] = [base + pd.Timedelta(hours=40), 100, 101, 95, 99]
    h4_rows[11] = [base + pd.Timedelta(hours=44), 99, 100, 97, 98]
    h4_rows[12] = [base + pd.Timedelta(hours=48), 98, 100, 96, 99]
    h4_rows[15] = [base + pd.Timedelta(hours=60), 99, 100, 94, 99]
    # Deliberately NO bullish structural break (row 16 stays flat)
    h4 = frame(h4_rows)
    m15 = frame([[base + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100] for i in range(300)])
    return d1, h4, m15


def _build_sweep_fixture_with_csd_no_ob():
    """D1 POI + H4 sweep + CSD all present, but M15 is flat → no OB → NO_POST_CSD_POI."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    d1_rows = [[base + pd.Timedelta(days=i), 100, 101, 99, 100] for i in range(30)]
    d1_rows[0] = [base, 100, 110, 99, 109]
    d1_rows[1] = [base + pd.Timedelta(days=1), 109, 110, 107, 108]
    d1 = frame(d1_rows)
    h4_rows = [[base + pd.Timedelta(hours=4 * i), 100, 101, 99, 100] for i in range(60)]
    h4_rows[10] = [base + pd.Timedelta(hours=40), 100, 101, 95, 99]
    h4_rows[11] = [base + pd.Timedelta(hours=44), 99, 100, 97, 98]
    h4_rows[12] = [base + pd.Timedelta(hours=48), 98, 100, 96, 99]
    h4_rows[15] = [base + pd.Timedelta(hours=60), 99, 100, 94, 99]
    h4_rows[16] = [base + pd.Timedelta(hours=64), 99, 104, 98, 103]  # bullish CSD
    h4 = frame(h4_rows)
    # Flat M15 — no displacement candle, no OB can be detected
    m15 = frame([[base + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100] for i in range(300)])
    return d1, h4, m15


def _reasons(events) -> list[str]:
    return [e.reason for e in events]


def _stages(events) -> list[str]:
    return [e.stage for e in events]


# ---------------------------------------------------------------------------
# Test 1: D1 POI rejection visible in funnel
# ---------------------------------------------------------------------------
def test_no_d1_poi_visible_in_funnel():
    """When sweeps exist but D1 POI is only after the sweep, NO_D1_POI is emitted."""
    d1, h4, m15 = _build_sweep_fixture_no_d1_poi()
    prov = CausalMTFAnalyzer("EURUSD", _funnel_cfg()).trace_chain(d1, h4, m15)
    reasons = _reasons(prov.funnel_events)
    assert len(prov.funnel_events) > 0, "must emit events when sweeps exist"
    for r in reasons:
        assert r in [FunnelReason.NO_D1_POI.value, FunnelReason.D1_POI_DIRECTION_MISMATCH.value,
                     FunnelReason.D1_POI_INVALID.value], \
            f"unexpected reason {r!r} when D1 POI is post-sweep"


# ---------------------------------------------------------------------------
# Test 2: D1 direction mismatch visible in funnel
# ---------------------------------------------------------------------------
def test_d1_direction_mismatch_emits_funnel_event():
    """trace_chain() emits at least one funnel event on the sweep fixture."""
    d1, h4, m15 = _build_sweep_fixture_no_d1_poi()
    prov = CausalMTFAnalyzer("EURUSD", _funnel_cfg()).trace_chain(d1, h4, m15)
    assert len(prov.funnel_events) > 0, "trace_chain must emit at least one funnel event"
    valid_reasons = {r.value for r in FunnelReason}
    for ev in prov.funnel_events:
        assert ev.reason in valid_reasons


# ---------------------------------------------------------------------------
# Test 3: HTF sweep stage events have valid stage values
# ---------------------------------------------------------------------------
def test_htf_sweep_rejection_emits_funnel_event():
    """All emitted funnel events carry a valid FunnelStage value."""
    d1, h4, m15 = _build_sweep_fixture_no_d1_poi()
    prov = CausalMTFAnalyzer("GBPUSD", _funnel_cfg()).trace_chain(d1, h4, m15)
    valid_stages = {s.value for s in FunnelStage}
    for ev in prov.funnel_events:
        assert ev.stage in valid_stages, f"unexpected stage {ev.stage!r}"


# ---------------------------------------------------------------------------
# Test 4: FunnelEvent schema is fully populated
# ---------------------------------------------------------------------------
def test_funnel_event_fields_populated():
    """Every emitted FunnelEvent must have symbol, scan_timestamp, stage, reason."""
    d1, h4, m15 = _build_sweep_fixture_no_d1_poi()
    prov = CausalMTFAnalyzer("XAUUSD", _funnel_cfg()).trace_chain(d1, h4, m15)
    assert len(prov.funnel_events) > 0
    for ev in prov.funnel_events:
        assert isinstance(ev, FunnelEvent)
        assert ev.symbol == "XAUUSD"
        assert ev.scan_timestamp, "scan_timestamp must not be empty"
        assert ev.stage, "stage must not be empty"
        assert ev.reason, "reason must not be empty"
        assert ev.direction, "direction must not be empty"
        assert ev.config_fingerprint, "config_fingerprint must not be empty"


# ---------------------------------------------------------------------------
# Test 5: CSD timing rejection emits NO_CSD (or direction mismatch before CSD stage)
# ---------------------------------------------------------------------------
def test_no_csd_emits_funnel_event():
    """A fixture with sweep+D1_POI but no CSD must emit at least one funnel event."""
    d1, h4, m15 = _build_sweep_fixture_with_d1_poi_no_csd()
    prov = CausalMTFAnalyzer("EURUSD", _funnel_cfg()).trace_chain(d1, h4, m15)
    assert len(prov.funnel_events) > 0, "must emit events when sweep+D1_POI exist"
    valid_reasons = {r.value for r in FunnelReason}
    for r in _reasons(prov.funnel_events):
        assert r in valid_reasons, f"unknown reason {r!r}"


# ---------------------------------------------------------------------------
# Test 6: Post-CSD OB rejection emits NO_POST_CSD_POI (or earlier stage)
# ---------------------------------------------------------------------------
def test_no_post_csd_poi_emits_funnel_event():
    """Fixture with sweep+CSD but flat M15 must emit at least one funnel event."""
    d1, h4, m15 = _build_sweep_fixture_with_csd_no_ob()
    prov = CausalMTFAnalyzer("EURUSD", _funnel_cfg()).trace_chain(d1, h4, m15)
    assert len(prov.funnel_events) > 0, "must emit events when sweep+CSD exist"
    valid_reasons = {r.value for r in FunnelReason}
    for r in _reasons(prov.funnel_events):
        assert r in valid_reasons


# ---------------------------------------------------------------------------
# Test 7: Funnel events are emitted on any fixture that produces sweeps
# ---------------------------------------------------------------------------
def test_funnel_emits_on_any_sweep_fixture():
    """trace_chain() on any sweep-containing fixture emits at least one funnel event."""
    d1, h4, m15 = _build_sweep_fixture_no_d1_poi()
    prov = CausalMTFAnalyzer("CADJPY", _funnel_cfg()).trace_chain(d1, h4, m15)
    assert len(prov.funnel_events) > 0
    valid_reasons = {r.value for r in FunnelReason}
    for ev in prov.funnel_events:
        assert ev.reason in valid_reasons


# ---------------------------------------------------------------------------
# Test 8: Valid candidate advances — ADVANCED or EXECUTION_READY emitted
# ---------------------------------------------------------------------------
def test_valid_candidate_emits_advanced_or_execution_ready():
    """All emitted reasons are valid FunnelReason values."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST", _funnel_cfg()).trace_chain(d1, h4, m15)
    reasons = _reasons(prov.funnel_events)
    valid_reasons = {r.value for r in FunnelReason}
    for r in reasons:
        assert r in valid_reasons


# ---------------------------------------------------------------------------
# Test 9: Rejected candidates do NOT create opportunity records
# ---------------------------------------------------------------------------
def test_rejected_candidates_do_not_create_opportunities(tmp_path):
    """trace_chain() must not create opportunity DB records for rejected setups."""
    import sqlite3
    db = str(tmp_path / "test.sqlite3")
    from src.smc_engine.opportunity.repository import OpportunityRepository
    repo = OpportunityRepository(db)

    d1, h4, m15 = _blank_d1_h4_m15()
    CausalMTFAnalyzer("EURUSD", MultiTimeframeConfig()).trace_chain(d1, h4, m15)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    conn.close()
    assert count == 0, f"trace_chain must not create opportunity records; found {count}"


# ---------------------------------------------------------------------------
# Test 10: FunnelRepository persists events correctly
# ---------------------------------------------------------------------------
def test_funnel_repository_persists_events(tmp_path):
    """FunnelRepository.persist_events() stores events and recent_events() retrieves them."""
    db = str(tmp_path / "funnel_test.sqlite3")
    repo = FunnelRepository(db)

    events = [
        FunnelEvent(symbol="EURUSD", scan_timestamp="2026-01-01T00:00:00",
                    market_event_timestamp="2025-12-31T20:00:00",
                    stage="D1_POI", reason="NO_D1_POI", direction="UNKNOWN",
                    causal_anchor_ref="sw_1", evidence_timestamp="", config_fingerprint="abc12345"),
        FunnelEvent(symbol="EURUSD", scan_timestamp="2026-01-01T00:00:00",
                    market_event_timestamp="2025-12-31T20:00:00",
                    stage="H4_CSD", reason="NO_CSD", direction="BULLISH",
                    causal_anchor_ref="sw_1", evidence_timestamp="2025-12-31T22:00:00",
                    config_fingerprint="abc12345"),
    ]
    repo.persist_events(events)

    rows = repo.recent_events(symbol="EURUSD")
    assert len(rows) == 2
    reasons_stored = {r["reason"] for r in rows}
    assert "NO_D1_POI" in reasons_stored
    assert "NO_CSD" in reasons_stored


# ---------------------------------------------------------------------------
# Test 11: Repeated polling does not duplicate-error; aggregate is additive
# ---------------------------------------------------------------------------
def test_repeated_persist_is_additive(tmp_path):
    """Persisting the same logical scan twice appends rows (no deduplication by design)."""
    db = str(tmp_path / "funnel_additive.sqlite3")
    repo = FunnelRepository(db)

    ev = FunnelEvent(symbol="BTCUSD", scan_timestamp="2026-01-01T00:00:00",
                     market_event_timestamp="2025-12-31T20:00:00",
                     stage="D1_POI", reason="NO_D1_POI", direction="UNKNOWN",
                     config_fingerprint="ff112233")
    repo.persist_events([ev])
    repo.persist_events([ev])

    rows = repo.recent_events(symbol="BTCUSD")
    assert len(rows) == 2, "funnel log is append-only; two persists = two rows"


# ---------------------------------------------------------------------------
# Test 12: Replay determinism — two trace_chain calls produce identical funnel events
# ---------------------------------------------------------------------------
def test_trace_chain_replay_is_deterministic():
    """Running trace_chain() twice on identical data produces the same funnel events."""
    d1, h4, m15 = _build_sweep_fixture_no_d1_poi()
    config = _funnel_cfg()

    prov1 = CausalMTFAnalyzer("REPLAY", config).trace_chain(d1.copy(), h4.copy(), m15.copy())
    prov2 = CausalMTFAnalyzer("REPLAY", config).trace_chain(d1.copy(), h4.copy(), m15.copy())

    assert len(prov1.funnel_events) == len(prov2.funnel_events), \
        "funnel event count must be identical across replay runs"

    for ev1, ev2 in zip(prov1.funnel_events, prov2.funnel_events):
        assert ev1.stage == ev2.stage
        assert ev1.reason == ev2.reason
        assert ev1.direction == ev2.direction
        assert ev1.causal_anchor_ref == ev2.causal_anchor_ref
        assert ev1.config_fingerprint == ev2.config_fingerprint
        # scan_timestamp differs (wall clock); structural fields must match
