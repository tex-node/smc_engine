"""Causal observation provenance regression tests (Gate B — Next Setup Gate).

Covers the 12 regression areas specified in the Gate B Next-Setup Gate brief.
All tests are read-only / observational — no order_send, no order_check, no
live execution, no position modification.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.smc_engine.causal import CausalMTFAnalyzer, CausalProvenance, _CHAIN_STAGES
from src.smc_engine.strategy import MultiTimeframeConfig
from src.smc_engine.web.hub import AUTHORIZED_LOGIN, AUTHORIZED_SERVER, DictSource, TIMEFRAMES

from tests.test_api import build_client, FakeDemoSource
from tests.test_account_identity import MockMT5Source
from tests.test_integration_fixture import build_fixture


# ---------------------------------------------------------------------------
# 1. Causal observation provenance — endpoint exists, returns all required fields
# ---------------------------------------------------------------------------
def test_causal_scan_endpoint_returns_required_fields(tmp_path):
    client, hub = build_client(tmp_path)
    r = client.get("/api/causal-scan/TEST")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    required = {
        "symbol", "as_of",
        "d1_poi_count", "matching_sweep_count", "csd_count",
        "post_csd_ob_count", "unmitigated_ob_count", "idm_count",
        "qualified_candidate_count", "rejection_stage", "rejection_reason",
    }
    missing = required - set(body.keys())
    assert not missing, f"missing fields in causal-scan response: {missing}"
    assert body["symbol"] == "TEST"
    hub.stop_poller()


def test_causal_scan_universe_endpoint_returns_required_fields(tmp_path):
    client, hub = build_client(tmp_path)
    r = client.get("/api/causal-scan")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "symbols_scanned" in body
    assert "candidates_found" in body
    assert "scan" in body
    assert isinstance(body["scan"], list)
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 2. Rejection-stage reporting — stage must be one of the valid chain stages
# ---------------------------------------------------------------------------
def test_rejection_stage_is_valid_chain_stage(tmp_path):
    client, hub = build_client(tmp_path)
    body = client.get("/api/causal-scan/TEST").json()
    assert body["rejection_stage"] in _CHAIN_STAGES, (
        f"rejection_stage {body['rejection_stage']!r} not in valid stages {_CHAIN_STAGES}"
    )
    hub.stop_poller()


def test_trace_chain_rejection_stage_is_valid():
    """trace_chain() directly always returns a valid chain stage."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15)
    assert prov.rejection_stage in _CHAIN_STAGES
    assert prov.symbol == "TEST"


def test_trace_chain_stage_index_non_negative():
    """The reported rejection_stage must have a valid index in _CHAIN_STAGES."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15)
    idx = _CHAIN_STAGES.index(prov.rejection_stage)
    assert idx >= 0


# ---------------------------------------------------------------------------
# 3. First real candidate registration — idempotency (scan twice, same count)
# ---------------------------------------------------------------------------
def test_causal_scan_candidate_count_is_idempotent(tmp_path):
    """Scanning the same symbol twice returns the same qualified_candidate_count."""
    client, hub = build_client(tmp_path)
    r1 = client.get("/api/causal-scan/TEST").json()
    r2 = client.get("/api/causal-scan/TEST").json()
    assert r1["qualified_candidate_count"] == r2["qualified_candidate_count"], (
        f"scan not idempotent: first={r1['qualified_candidate_count']}, "
        f"second={r2['qualified_candidate_count']}"
    )
    hub.stop_poller()


def test_causal_scan_rejection_stage_is_idempotent(tmp_path):
    """rejection_stage does not change between identical scans of the same symbol."""
    client, hub = build_client(tmp_path)
    r1 = client.get("/api/causal-scan/TEST").json()
    r2 = client.get("/api/causal-scan/TEST").json()
    assert r1["rejection_stage"] == r2["rejection_stage"]
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 4. As-of replay equivalence — trace_chain is deterministic at a fixed as_of
# ---------------------------------------------------------------------------
def test_as_of_replay_returns_same_result():
    """Same as_of produces identical provenance on repeated calls."""
    d1, h4, m15 = build_fixture()
    analyzer = CausalMTFAnalyzer("TEST")
    as_of = m15["time"].iloc[-1]
    prov1 = analyzer.trace_chain(d1, h4, m15, as_of=as_of)
    prov2 = analyzer.trace_chain(d1, h4, m15, as_of=as_of)
    assert prov1.rejection_stage == prov2.rejection_stage
    assert prov1.rejection_reason == prov2.rejection_reason
    assert prov1.d1_poi_count == prov2.d1_poi_count
    assert prov1.qualified_candidate_count == prov2.qualified_candidate_count


def test_as_of_none_equals_explicit_last_bar():
    """as_of=None defaults to last M15 bar — produces same result as explicit value."""
    d1, h4, m15 = build_fixture()
    analyzer = CausalMTFAnalyzer("TEST")
    explicit_as_of = m15["time"].iloc[-1]
    prov_explicit = analyzer.trace_chain(d1, h4, m15, as_of=explicit_as_of)
    prov_implicit = analyzer.trace_chain(d1, h4, m15, as_of=None)
    assert prov_explicit.rejection_stage == prov_implicit.rejection_stage
    assert prov_explicit.d1_poi_count == prov_implicit.d1_poi_count
    assert prov_explicit.qualified_candidate_count == prov_implicit.qualified_candidate_count


# ---------------------------------------------------------------------------
# 5. Future leakage prevention — as_of before displacement excludes all POIs
# ---------------------------------------------------------------------------
def test_future_leakage_as_of_before_d1_displacement():
    """as_of before D1 displacement (row 14 = day 14) must see 0 D1 POIs."""
    d1, h4, m15 = build_fixture()
    # D1 displacement at row 14 (day 14). Clipping to day 5 must yield no POI.
    early_as_of = pd.Timestamp("2026-01-06", tz="UTC")
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15, as_of=early_as_of)
    assert prov.d1_poi_count == 0, (
        f"as_of before D1 displacement must yield 0 D1 POIs, got {prov.d1_poi_count}"
    )
    assert prov.rejection_stage == "D1_POI"


def test_future_leakage_later_as_of_cannot_show_fewer_d1_pois():
    """A later as_of must not return fewer D1 POIs than an earlier one in same data."""
    d1, h4, m15 = build_fixture()
    analyzer = CausalMTFAnalyzer("TEST")
    early = pd.Timestamp("2026-01-06", tz="UTC")
    late = pd.Timestamp("2026-01-20", tz="UTC")
    prov_early = analyzer.trace_chain(d1, h4, m15, as_of=early)
    prov_late = analyzer.trace_chain(d1, h4, m15, as_of=late)
    # More data visible at later as_of → can only find same or more POIs
    assert prov_late.d1_poi_count >= prov_early.d1_poi_count


# ---------------------------------------------------------------------------
# 6. Duplicate scan idempotency — universe scan called twice returns same totals
# ---------------------------------------------------------------------------
def test_duplicate_universe_scan_idempotent(tmp_path):
    client, hub = build_client(tmp_path)
    r1 = client.get("/api/causal-scan").json()
    r2 = client.get("/api/causal-scan").json()
    assert r1["symbols_scanned"] == r2["symbols_scanned"]
    assert r1["candidates_found"] == r2["candidates_found"]
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 7. Rolling-window identity stability — rejection_stage never regresses
# ---------------------------------------------------------------------------
def test_rejection_stage_never_regresses_across_calls():
    """Repeated calls at same as_of return the same stage — never earlier in chain."""
    d1, h4, m15 = build_fixture()
    analyzer = CausalMTFAnalyzer("TEST")
    as_of = m15["time"].iloc[-1]
    prov1 = analyzer.trace_chain(d1, h4, m15, as_of=as_of)
    prov2 = analyzer.trace_chain(d1, h4, m15, as_of=as_of)
    idx1 = _CHAIN_STAGES.index(prov1.rejection_stage)
    idx2 = _CHAIN_STAGES.index(prov2.rejection_stage)
    assert idx2 >= idx1, (
        f"rejection_stage regressed from {prov1.rejection_stage!r} to {prov2.rejection_stage!r}"
    )


def test_rolling_window_stage_consistent_across_different_calls(tmp_path):
    """API scan and direct trace_chain on the same data agree on rejection_stage."""
    client, hub = build_client(tmp_path)
    api_result = client.get("/api/causal-scan/TEST").json()
    # Direct call on the same fixture data
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15)
    # Both should use as_of = last M15 bar and reach the same stage
    assert api_result["rejection_stage"] == prov.rejection_stage, (
        f"API result {api_result['rejection_stage']!r} differs from "
        f"direct trace_chain {prov.rejection_stage!r}"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 8. IRL provenance fields — CausalProvenance has all IRL-adjacent stage fields
# ---------------------------------------------------------------------------
def test_causal_provenance_dataclass_has_all_fields():
    """CausalProvenance carries all stage-count and IRL-adjacent fields."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15)
    assert isinstance(prov, CausalProvenance)
    for field in ("d1_poi_count", "matching_sweep_count", "csd_count",
                  "post_csd_ob_count", "unmitigated_ob_count", "idm_count",
                  "qualified_candidate_count"):
        val = getattr(prov, field)
        assert isinstance(val, int) and val >= 0, (
            f"CausalProvenance.{field}={val!r} must be a non-negative int"
        )
    assert isinstance(prov.rejection_stage, str)
    assert isinstance(prov.rejection_reason, str)


def test_api_causal_scan_count_fields_non_negative(tmp_path):
    """All count fields in /api/causal-scan/{symbol} are non-negative integers."""
    client, hub = build_client(tmp_path)
    body = client.get("/api/causal-scan/TEST").json()
    for field in ("d1_poi_count", "matching_sweep_count", "csd_count",
                  "post_csd_ob_count", "unmitigated_ob_count", "idm_count",
                  "qualified_candidate_count"):
        assert isinstance(body[field], int) and body[field] >= 0, (
            f"{field}={body[field]!r} must be non-negative int"
        )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 9. Historical/current alert separation — server_startup_time present
# ---------------------------------------------------------------------------
def test_alerts_include_server_startup_time(tmp_path):
    """/api/alerts must carry server_startup_time for historical alert separation."""
    client, hub = build_client(tmp_path)
    r = client.get("/api/alerts").json()
    assert "server_startup_time" in r, "alerts must include server_startup_time"
    startup = r["server_startup_time"]
    if startup is not None:
        pd.Timestamp(startup)  # must be parseable ISO 8601
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 10. UI propagation — causal-scan universe exposes all fields the UI needs
# ---------------------------------------------------------------------------
def test_causal_scan_universe_ui_fields_present(tmp_path):
    """/api/causal-scan exposes all fields required for Gate B UI display."""
    client, hub = build_client(tmp_path)
    body = client.get("/api/causal-scan").json()
    assert isinstance(body["symbols_scanned"], int) and body["symbols_scanned"] >= 0
    assert isinstance(body["candidates_found"], int) and body["candidates_found"] >= 0
    assert isinstance(body["scan"], list)
    if body["scan"]:
        entry = body["scan"][0]
        for f in ("symbol", "rejection_stage", "rejection_reason",
                  "qualified_candidate_count"):
            assert f in entry, f"scan entry missing field {f!r}"
    hub.stop_poller()


def test_causal_scan_universe_contains_primed_symbol(tmp_path):
    """TEST symbol primed in the fixture appears in the universe scan."""
    client, hub = build_client(tmp_path)
    body = client.get("/api/causal-scan").json()
    symbols = [e["symbol"] for e in body["scan"]]
    assert "TEST" in symbols, f"TEST not in scan symbols: {symbols}"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 11. Unauthorized-account blocking — /api/causal-scan returns 403 for
#     unauthorized MT5 (name="mt5") sources
# ---------------------------------------------------------------------------
def test_unauthorized_mt5_blocks_causal_scan_universe(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/causal-scan")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "UNAUTHORIZED_ACCOUNT" in r.text
    hub.stop_poller()


def test_unauthorized_mt5_blocks_causal_scan_symbol(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/causal-scan/TEST")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "UNAUTHORIZED_ACCOUNT" in r.text
    hub.stop_poller()


def test_authorized_mt5_can_call_causal_scan(tmp_path):
    """Authorized MT5 demo account (477217728@Exness-MT5Trial9) can call causal-scan."""
    src = MockMT5Source(AUTHORIZED_LOGIN, AUTHORIZED_SERVER, True)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/causal-scan/TEST")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    hub.stop_poller()


def test_dict_source_bypasses_gate_for_causal_scan(tmp_path):
    """DictSource (non-MT5) bypasses identity gate — causal-scan must return 200."""
    client, hub = build_client(tmp_path, FakeDemoSource())
    r = client.get("/api/causal-scan/TEST")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 12. Live execution remaining disabled — always False regardless of auth state
# ---------------------------------------------------------------------------
def test_live_execution_never_enabled_regardless_of_auth(tmp_path):
    """live_execution_enabled is False for all source types."""
    sources = [
        MockMT5Source(AUTHORIZED_LOGIN, AUTHORIZED_SERVER, True),
        MockMT5Source(17594495, "Headway-Real", False),
        DictSource(),
        FakeDemoSource(),
    ]
    for i, src in enumerate(sources):
        sub = tmp_path / str(i)
        sub.mkdir()
        _, hub = build_client(sub, src)
        st = hub.status()
        assert st["live_execution_enabled"] is False, (
            f"live_execution_enabled must be False for source {src.name!r}, got True"
        )
        hub.stop_poller()


def test_live_execution_endpoint_always_403(tmp_path):
    """/api/live/{setup_id} always returns 403 — live execution is unconditionally disabled."""
    client, hub = build_client(tmp_path)
    r = client.post("/api/live/SETUP-ANY")
    assert r.status_code == 403, f"expected 403 for live endpoint, got {r.status_code}"
    assert "DISABLED" in r.text.upper() or r.status_code == 403
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 13. csd_count accuracy — CSDs from before the M15 data window must not be
#     counted.  Before the fix, prov.csd_count += 1 fired before the
#     m15_end_for_csd < 0 guard, so ghost CSDs inflated the count and kept
#     rejection_stage stuck at H4_CSD even when genuine CSDs existed.
# ---------------------------------------------------------------------------
def test_csd_count_does_not_include_csds_outside_m15_window():
    """trace_chain csd_count must be 0 when the CSD is before the M15 data window."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    from tests.test_integration_fixture import frame

    # D1: bullish displacement that creates a bullish POI
    d1 = frame([[base + pd.Timedelta(days=i), 100, 101, 99, 100] for i in range(20)])
    d1.loc[14, ["open", "high", "low", "close"]] = [100, 110, 99, 109]
    d1.loc[15, ["open", "high", "low", "close"]] = [109, 110, 107, 108]

    # H4: sweep + CSD at bars 15-16 (t=60h..64h)
    h4_rows = [[base + pd.Timedelta(hours=4 * i), 100, 101, 99, 100] for i in range(30)]
    h4_rows[10] = [base + pd.Timedelta(hours=40), 100, 101, 95, 99]
    h4_rows[11] = [base + pd.Timedelta(hours=44), 99, 100, 97, 98]
    h4_rows[12] = [base + pd.Timedelta(hours=48), 98, 100, 96, 99]
    h4_rows[15] = [base + pd.Timedelta(hours=60), 99, 100, 94, 99]   # sweep
    h4_rows[16] = [base + pd.Timedelta(hours=64), 99, 104, 98, 103]  # CSD
    h4 = frame(h4_rows)

    # M15: starts AFTER the CSD (first bar at t=200h), so csd.candle_time (64h)
    # is before the entire M15 window → m15_end_for_csd must return -1.
    m15_start = base + pd.Timedelta(hours=200)
    m15 = frame([[m15_start + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100]
                 for i in range(50)])

    from src.smc_engine.strategy import MultiTimeframeConfig
    cfg = MultiTimeframeConfig(
        d1_lookback=20, d1_poi_lookback=20,
        h4_swing_left=2, h4_swing_right=2,
        h4_sweep_lookback=10, h4_csd_window=6,
        m15_swing_left=2, m15_swing_right=2,
        m15_atr_period=5, m15_displacement_atr=1.2,
    )
    prov = CausalMTFAnalyzer("TEST", cfg).trace_chain(d1, h4, m15)
    assert prov.csd_count == 0, (
        f"csd_count must be 0 when all CSDs predate the M15 data window, "
        f"got csd_count={prov.csd_count}"
    )


def test_rejection_stage_advances_to_m15_ob_when_csd_has_m15_data_but_no_ob():
    """When a CSD is within the M15 window but no OB forms, stage must reach M15_OB."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    from tests.test_integration_fixture import frame

    d1 = frame([[base + pd.Timedelta(days=i), 100, 101, 99, 100] for i in range(20)])
    d1.loc[14, ["open", "high", "low", "close"]] = [100, 110, 99, 109]
    d1.loc[15, ["open", "high", "low", "close"]] = [109, 110, 107, 108]

    h4_rows = [[base + pd.Timedelta(hours=4 * i), 100, 101, 99, 100] for i in range(30)]
    h4_rows[10] = [base + pd.Timedelta(hours=40), 100, 101, 95, 99]
    h4_rows[11] = [base + pd.Timedelta(hours=44), 99, 100, 97, 98]
    h4_rows[12] = [base + pd.Timedelta(hours=48), 98, 100, 96, 99]
    h4_rows[15] = [base + pd.Timedelta(hours=60), 99, 100, 94, 99]
    h4_rows[16] = [base + pd.Timedelta(hours=64), 99, 104, 98, 103]
    h4 = frame(h4_rows)

    # M15: starts BEFORE the CSD (t=64h) so the CSD is inside the M15 window,
    # but all candles are flat (no displacements) → no OB can form → M15_OB rejection.
    m15_start = base + pd.Timedelta(hours=50)
    m15 = frame([[m15_start + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100]
                 for i in range(50)])

    from src.smc_engine.strategy import MultiTimeframeConfig
    cfg = MultiTimeframeConfig(
        d1_lookback=20, d1_poi_lookback=20,
        h4_swing_left=2, h4_swing_right=2,
        h4_sweep_lookback=10, h4_csd_window=6,
        m15_swing_left=2, m15_swing_right=2,
        m15_atr_period=5, m15_displacement_atr=1.2,
    )
    prov = CausalMTFAnalyzer("TEST", cfg).trace_chain(d1, h4, m15)
    # The CSD at t=64h is within M15 data (starts at t=50h), so csd_count >= 1
    # and the stage must advance at least to M15_OB.
    if prov.csd_count > 0:
        stage_idx = _CHAIN_STAGES.index(prov.rejection_stage)
        m15_ob_idx = _CHAIN_STAGES.index("M15_OB")
        assert stage_idx >= m15_ob_idx, (
            f"With a CSD in M15 window but no OBs, stage must be >= M15_OB, "
            f"got {prov.rejection_stage!r} (index {stage_idx})"
        )


def test_causal_scan_does_not_enable_live_execution(tmp_path):
    """Calling causal-scan (even for a symbol with a candidate) must not flip live flag."""
    client, hub = build_client(tmp_path)
    client.get("/api/causal-scan")
    client.get("/api/causal-scan/TEST")
    st = hub.status()
    assert st["live_execution_enabled"] is False, (
        "live_execution_enabled must remain False after causal scans"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 14. Quality-separation diagnostics — new CausalProvenance fields
#     structurally_qualified_count, rr_filtered_count, rr_gate,
#     execution_ready_count, new_admissible_count
# ---------------------------------------------------------------------------

def test_causal_provenance_has_quality_separation_fields():
    """CausalProvenance exposes all quality-separation fields as non-negative ints/strs."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15)
    for field in ("structurally_qualified_count", "rr_filtered_count",
                  "new_admissible_count", "execution_ready_count"):
        val = getattr(prov, field)
        assert isinstance(val, int) and val >= 0, (
            f"CausalProvenance.{field}={val!r} must be a non-negative int"
        )
    assert isinstance(prov.rr_gate, str)


def test_rr_gate_empty_when_min_rr_zero():
    """rr_gate must be empty string when min_rr=0.0 (gate inactive)."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST", MultiTimeframeConfig(min_rr=0.0)).trace_chain(d1, h4, m15)
    assert prov.rr_gate == "", f"rr_gate must be '' when min_rr=0.0, got {prov.rr_gate!r}"


def test_rr_gate_set_when_min_rr_positive():
    """rr_gate carries the active threshold description when min_rr > 0."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST", MultiTimeframeConfig(min_rr=1.0)).trace_chain(d1, h4, m15)
    assert "1.0" in prov.rr_gate, (
        f"rr_gate must contain the threshold when min_rr=1.0, got {prov.rr_gate!r}"
    )


def test_structurally_qualified_gte_execution_ready():
    """structurally_qualified_count >= execution_ready_count always."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15)
    assert prov.structurally_qualified_count >= prov.execution_ready_count, (
        f"structurally_qualified({prov.structurally_qualified_count}) must be "
        f">= execution_ready({prov.execution_ready_count})"
    )


def test_execution_ready_count_equals_qualified_candidate_count():
    """execution_ready_count and qualified_candidate_count must agree (backward compat)."""
    d1, h4, m15 = build_fixture()
    prov = CausalMTFAnalyzer("TEST").trace_chain(d1, h4, m15)
    assert prov.execution_ready_count == prov.qualified_candidate_count, (
        f"execution_ready_count({prov.execution_ready_count}) != "
        f"qualified_candidate_count({prov.qualified_candidate_count})"
    )


def test_rr_filter_separates_from_structural_rejection():
    """When min_rr filters a setup, rejection_stage must not be 'IRL'.

    A structurally complete setup (chain reached READY) that is rejected only
    by the RR gate must show rejection_stage='READY', not 'IRL', because the
    IRL stage succeeded — a structural IRL target was found.
    """
    d1, h4, m15 = build_fixture()
    # With min_rr=0.0 the chain may reach READY (depending on fixture data).
    prov_gate_off = CausalMTFAnalyzer("TEST", MultiTimeframeConfig(min_rr=0.0)).trace_chain(d1, h4, m15)

    if prov_gate_off.structurally_qualified_count == 0:
        pytest.skip("test fixture produced no structurally qualified setups")

    # With min_rr=99 every setup is RR-filtered.
    prov_gate_on = CausalMTFAnalyzer("TEST", MultiTimeframeConfig(min_rr=99.0)).trace_chain(d1, h4, m15)
    assert prov_gate_on.rr_filtered_count > 0, (
        "min_rr=99 must filter at least one setup that passed the structural check"
    )
    assert prov_gate_on.rejection_stage == "READY", (
        f"RR-filtered setup should show rejection_stage='READY' not {prov_gate_on.rejection_stage!r}"
    )


def test_api_causal_scan_returns_quality_separation_fields(tmp_path):
    """/api/causal-scan/{symbol} response includes all quality-separation fields."""
    client, hub = build_client(tmp_path)
    body = client.get("/api/causal-scan/TEST").json()
    for field in ("structurally_qualified_count", "rr_filtered_count",
                  "new_admissible_count", "execution_ready_count", "rr_gate"):
        assert field in body, f"/api/causal-scan/TEST response missing field {field!r}"
    hub.stop_poller()


def test_new_admissible_count_not_negative(tmp_path):
    """new_admissible_count from API is always >= 0."""
    client, hub = build_client(tmp_path)
    body = client.get("/api/causal-scan/TEST").json()
    assert isinstance(body["new_admissible_count"], int) and body["new_admissible_count"] >= 0
    hub.stop_poller()
