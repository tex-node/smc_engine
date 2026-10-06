"""
IRL Target Qualification regression suite.

Tests:
  1. OB-adjacent swing rejected even when it satisfies the ATR threshold
  2. Meaningful internal swing selected (scoring favours strength × distance)
  3. Deeper/stronger swing not preferred when a nearer meaningful pool exists
  4. Already-consumed liquidity rejected (level traded through before as_of)
  5. Bullish mirror: confirmed swing HIGH above entry selected
  6. Bearish mirror: confirmed swing LOW below entry selected
  7. No valid target returns NO_VALID_TARGET (None)
  8. Full-length and production-shaped datasets remain compatible
  9. Target selection is deterministic (identical inputs → identical output)
 10. No broker calls possible from the target-selection path
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.smc_engine.models import Direction, SwingPoint, SwingType
from src.smc_engine.setup import NO_VALID_TARGET, find_irl_target
from src.smc_engine.structure import find_swings


# ── helpers ───────────────────────────────────────────────────────────────────

def sp(
    idx: int,
    typ: SwingType,
    price: float,
    strength: int = 6,
    conf_idx: int | None = None,
) -> SwingPoint:
    ci = idx + 2 if conf_idx is None else conf_idx
    return SwingPoint(
        id=f"S{typ.value[0]}-{idx}",
        index=idx,
        time=idx,
        type=typ,
        price=float(price),
        strength=strength,
        confirmation_index=ci,
        confirmation_time=ci,
    )


def _ohlc_from_lows(lows: list[float], start_open: float = 105.0) -> pd.DataFrame:
    """Build minimal OHLC (time=row index, close=open=start_open, high=open+0.5)."""
    rows = []
    for i, lo in enumerate(lows):
        rows.append({
            "time": i,
            "open": start_open,
            "high": start_open + 0.5,
            "low": lo,
            "close": start_open,
        })
    return pd.DataFrame(rows)


# ── Test 1: OB-adjacent swing rejected (ob_proximity_bars hard reject) ────────

def test_ob_adjacent_swing_rejected_even_if_beyond_atr():
    """
    An OB-adjacent swing (|index - ob_candle_index| ≤ ob_proximity_bars) is
    hard-rejected even when its distance from entry exceeds the ATR threshold.
    The deeper, non-adjacent swing is selected instead.
    """
    entry = 111.175
    atr = 0.01          # very tight ATR so 111.159 passes the ATR filter
    ob_idx = 276

    swings = [
        sp(277, SwingType.LOW, 111.159, strength=6),   # OB-adjacent (|277-276|=1)
        sp(200, SwingType.LOW, 110.420, strength=6),   # meaningful internal low
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        min_distance=atr,
        ob_candle_index=ob_idx,
        ob_proximity_bars=10,
    )

    assert result is not None, "Expected a valid target (110.42 qualifies)"
    assert result.price == pytest.approx(110.420), (
        f"OB-adjacent 111.159 must be rejected; expected 110.42, got {result.price}"
    )


# ── Test 2: Meaningful internal swing selected by scoring ─────────────────────

def test_meaningful_internal_swing_selected_by_score():
    """
    Two non-adjacent swings both survive hard rejects. The one with higher
    strength × distance_factor score is chosen over the merely-nearest one.

    Swing A: strength=4, distance=0.1  → score=4*(1/10)=0.40
    Swing B: strength=6, distance=0.2  → score=6*(1/20)=0.30
    (ATR=0.01, so dist_atrs = dist/0.01)

    Wait — let me recalculate with ATR=0.01:
      A: dist_atrs=0.1/0.01=10, dist_score=1/10=0.1, score=4*0.1=0.40
      B: dist_atrs=0.2/0.01=20, dist_score=1/20=0.05, score=6*0.05=0.30

    Hmm, A wins. Let me adjust so B wins:
      A: strength=4, distance=0.5 ATRs → too close (< 1 ATR × 0.01 = filtered by min_distance)
    Actually min_distance = atr = 0.05.
      A: strength=2, distance=0.10  → dist_atrs=0.10/0.05=2.0, score=2*(1/2)=1.0
      B: strength=6, distance=0.30  → dist_atrs=0.30/0.05=6.0, score=6*(1/6)=1.0 — tie

    Let me use clearer values:
      A: strength=2, distance=0.10 ATR=0.05 → dist_atrs=2.0, score=2*(1/2)=1.0
      B: strength=6, distance=0.15 ATR=0.05 → dist_atrs=3.0, score=6*(1/3)=2.0 → B wins
    """
    entry = 111.175
    atr = 0.05

    swings = [
        sp(250, SwingType.LOW, 111.175 - 0.10, strength=2),  # nearer but weak
        sp(200, SwingType.LOW, 111.175 - 0.15, strength=6),  # farther but strong → wins
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        min_distance=atr,
        ob_candle_index=300,  # far from both swings
        ob_proximity_bars=10,
    )

    assert result is not None
    expected_tp = pytest.approx(111.175 - 0.15)
    assert result.price == expected_tp, (
        f"Higher-scoring stronger swing should be selected; got {result.price}"
    )
    assert result.target_strength == 6


# ── Test 3: Deeper swing not preferred over nearer meaningful pool ─────────────

def test_deeper_external_swing_not_preferred_over_internal():
    """
    A swing at 8+ ATRs (external territory) scores lower than a comparable
    swing at 3 ATRs (internal).  The internal swing wins.

    Swing A: strength=6, dist=3 ATRs  → score=6*(1/3)=2.0
    Swing B: strength=6, dist=10 ATRs → score=6*(1/10)*0.5=0.3 (>8 ATR penalty)
    """
    entry = 111.175
    atr = 0.10

    swings = [
        sp(200, SwingType.LOW, entry - 3 * atr, strength=6),   # 3 ATRs — internal
        sp(100, SwingType.LOW, entry - 10 * atr, strength=6),  # 10 ATRs — external
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        min_distance=atr,
        ob_candle_index=300,
    )

    assert result is not None
    assert result.price == pytest.approx(entry - 3 * atr), (
        f"Internal (3-ATR) swing should score above external (10-ATR); got {result.price}"
    )


# ── Test 4: Already-consumed liquidity rejected ───────────────────────────────

def test_consumed_liquidity_rejected():
    """
    A swing LOW at 110.42 is confirmed at index 50 and subsequently consumed
    (a candle at index 80 prints low = 110.40, breaching the swing level).
    Only the non-consumed swing at 110.00 (confirmed at 30, never breached) is
    eligible, so that is the selected TP.
    """
    entry = 111.175
    atr = 0.05

    # Build OHLC where index 80 has low=110.40 (breaches 110.42)
    lows = [111.5] * 100  # baseline high lows
    lows[80] = 110.40      # breaches 110.42 at index 80
    ohlc = _ohlc_from_lows(lows)

    consumed_swing = sp(48, SwingType.LOW, 110.420, strength=6, conf_idx=50)
    intact_swing   = sp(28, SwingType.LOW, 110.000, strength=6, conf_idx=30)

    result = find_irl_target(
        entry, Direction.BEARISH,
        [consumed_swing, intact_swing],
        ohlc=ohlc,
        as_of=90,          # as_of index 90: index 80 breach is visible
        min_distance=atr,
        ob_candle_index=300,
    )

    assert result is not None, "Non-consumed swing at 110.00 should qualify"
    assert result.price == pytest.approx(110.000), (
        f"Consumed swing at 110.42 must be rejected; expected 110.00, got {result.price}"
    )


# ── Test 5: Bullish mirror ─────────────────────────────────────────────────────

def test_bullish_ob_adjacent_rejected_internal_swing_selected():
    """
    Bullish mirror of Test 1: swing HIGH just above entry (OB-adjacent) is
    rejected; the meaningful internal HIGH above entry is selected.
    """
    entry = 100.0
    atr = 0.01
    ob_idx = 50

    swings = [
        sp(51, SwingType.HIGH, 100.02, strength=6),   # OB-adjacent (|51-50|=1)
        sp(20,  SwingType.HIGH, 101.50, strength=6),  # meaningful internal high
    ]

    result = find_irl_target(
        entry, Direction.BULLISH, swings,
        min_distance=atr,
        ob_candle_index=ob_idx,
        ob_proximity_bars=10,
    )

    assert result is not None
    assert result.price == pytest.approx(101.50)


# ── Test 6: Bearish mirror (reference) ───────────────────────────────────────

def test_bearish_strength_scored_selection():
    """
    Basic bearish case: three qualified candidates at different strengths and
    distances.  Verify the highest-scoring one is returned and provenance is set.
    """
    entry = 111.00
    atr = 0.05

    swings = [
        sp(250, SwingType.LOW, 110.85, strength=4),  # 3 ATRs, weak
        sp(200, SwingType.LOW, 110.70, strength=8),  # 6 ATRs, strong → wins
        sp(150, SwingType.LOW, 110.00, strength=6),  # 20 ATRs, external penalty
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        min_distance=atr,
        ob_candle_index=300,
    )

    assert result is not None
    assert result.price == pytest.approx(110.70)
    assert result.target_strength == 8
    assert "strength=8" in result.qualification_reason
    assert result.target_type == "INTERNAL_SWING"
    assert result.consumed is False


# ── Test 7: No valid target → NO_VALID_TARGET ─────────────────────────────────

def test_no_valid_target_returns_sentinel():
    """
    When every candidate fails qualification (all OB-adjacent or all consumed),
    find_irl_target must return NO_VALID_TARGET (None) rather than manufacturing
    a TP from the least-bad option.
    """
    entry = 111.175
    atr = 0.05
    ob_idx = 276

    swings = [
        sp(275, SwingType.LOW, 111.100, strength=6),  # OB-adjacent (|275-276|=1)
        sp(278, SwingType.LOW, 111.050, strength=6),  # OB-adjacent (|278-276|=2)
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        min_distance=atr,
        ob_candle_index=ob_idx,
        ob_proximity_bars=10,
    )

    assert result is NO_VALID_TARGET, (
        f"Expected NO_VALID_TARGET (None) when all candidates are OB-adjacent; got {result}"
    )


# ── Test 8: Full-length production-shaped dataset compatible ──────────────────

def test_production_shaped_dataset_compatible():
    """
    Pass a realistic production-shaped dataset through find_irl_target using
    the full new API and verify it returns a valid result or NO_VALID_TARGET
    without raising an exception.  This guards backward compatibility.
    """
    # Build 200 synthetic OHLC bars around 111.00
    import numpy as np
    rng = np.random.default_rng(42)
    prices = 111.0 + np.cumsum(rng.normal(0, 0.02, 200))
    ohlc_rows = []
    for i, p in enumerate(prices):
        ohlc_rows.append({
            "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=15 * i),
            "open": p,
            "high": p + abs(rng.normal(0, 0.03)),
            "low": p - abs(rng.normal(0, 0.03)),
            "close": p,
        })
    ohlc_df = pd.DataFrame(ohlc_rows)

    swings = find_swings(ohlc_df, left=2, right=2)
    entry = float(prices[150])
    atr = 0.05

    # Should not raise regardless of whether a target is found
    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        ohlc=ohlc_df,
        as_of=ohlc_df.iloc[170]["time"],
        min_distance=atr,
        ob_candle_index=150,
        ob_proximity_bars=10,
    )
    # result is either an IRLTarget or NO_VALID_TARGET — both are valid
    assert result is None or hasattr(result, "swing_id"), (
        f"Expected IRLTarget or None; got {type(result)}"
    )
    if result is not None:
        assert result.price < entry
        assert "strength=" in result.qualification_reason


# ── Test 9: Deterministic selection ──────────────────────────────────────────

def test_target_selection_is_deterministic():
    """
    Two calls with identical inputs must return identical results.  This guards
    against any non-deterministic sort or random element.
    """
    entry = 111.175
    atr = 0.05

    swings = [
        sp(200, SwingType.LOW, 110.420, strength=6),
        sp(180, SwingType.LOW, 110.100, strength=8),
        sp(160, SwingType.LOW, 109.800, strength=4),
    ]

    r1 = find_irl_target(entry, Direction.BEARISH, swings, min_distance=atr, ob_candle_index=300)
    r2 = find_irl_target(entry, Direction.BEARISH, swings, min_distance=atr, ob_candle_index=300)

    assert r1 is not None
    assert r1.swing_id == r2.swing_id
    assert r1.price == r2.price
    assert r1.qualification_reason == r2.qualification_reason


# ── Test 10: No broker calls possible from target-selection path ──────────────

def test_no_broker_calls_possible():
    """
    find_irl_target operates on pure Python / pandas data.  Calling it with
    synthetic inputs and no MT5 connection must succeed — confirming that no
    broker API is reachable from the qualification path.
    """
    import src.smc_engine.setup as setup_module

    # Module must not import MetaTrader5 or carry mt5 in its namespace
    assert not hasattr(setup_module, "mt5"), "setup.py must not import mt5"
    module_source_names = [k for k in vars(setup_module).keys()]
    assert "MetaTrader5" not in module_source_names

    # Calling the function must work without any MT5 connection
    swings = [sp(100, SwingType.LOW, 99.5, strength=6)]
    result = find_irl_target(100.0, Direction.BEARISH, swings, min_distance=0.01)

    assert result is not None, "Synthetic call must work without broker connection"
    assert result.price == pytest.approx(99.5)


# ── Adversarial suite (cases A–L from Gate B specification) ──────────────────


# ── Case E: Future leakage — swing confirmed after as_of must be excluded ─────

def test_future_swing_excluded_from_candidate_pool():
    """
    Case E — Future leakage.

    A swing confirmed after ``as_of`` must never enter the candidate pool even
    if it would score highest.  Only swings whose ``confirmation_time ≤ as_of``
    are eligible.

    Layout:
      * past_swing   confirmed at t=50  (valid, should be selected)
      * future_swing confirmed at t=200 (invalid — after as_of=100)
      future_swing has higher strength so it *would* win if the as_of filter
      were absent.
    """
    entry = 110.0
    atr = 0.10
    as_of = 100  # integer timestamp — both swings use integer times

    past_swing = sp(40, SwingType.LOW, 109.0, strength=4, conf_idx=50)   # before as_of ✓
    future_swing = sp(150, SwingType.LOW, 108.0, strength=10, conf_idx=200)  # after as_of ✗

    result = find_irl_target(
        entry, Direction.BEARISH,
        [past_swing, future_swing],
        as_of=as_of,
        min_distance=atr,
        ob_candle_index=500,
    )

    assert result is not None, "past_swing at 109.0 is a valid candidate"
    assert result.price == pytest.approx(109.0), (
        f"future_swing must be excluded despite higher strength; got {result.price}"
    )
    assert result.swing_id == past_swing.id


def test_all_future_swings_returns_no_valid_target():
    """
    Case E variant — When every swing is confirmed after as_of, no candidate
    pool exists and NO_VALID_TARGET is returned.
    """
    entry = 110.0
    atr = 0.10
    as_of = 10  # well before all confirmation times

    swings = [
        sp(50, SwingType.LOW, 109.0, strength=6, conf_idx=52),   # conf after as_of
        sp(60, SwingType.LOW, 108.5, strength=8, conf_idx=62),   # conf after as_of
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        as_of=as_of, min_distance=atr, ob_candle_index=500,
    )

    assert result is NO_VALID_TARGET, (
        "All swings are future-confirmation; pool must be empty → NO_VALID_TARGET"
    )


# ── Case A (extension): Weak micro swing rejected by min_strength threshold ───

def test_weak_micro_swing_rejected_by_min_strength():
    """
    Case A (strength extension).

    A nearby swing with strength=2 is rejected by ``min_strength=4``.
    A stronger swing at greater distance is the only surviving candidate and is
    therefore selected.

    This tests that min_strength enforces a structural-significance floor — tiny
    two-bar pivots cannot sneak in as TPs.
    """
    entry = 111.0
    atr = 0.05

    weak_swing  = sp(280, SwingType.LOW, 110.85, strength=2)  # too weak
    strong_swing = sp(200, SwingType.LOW, 110.50, strength=6)  # passes all rejects

    result = find_irl_target(
        entry, Direction.BEARISH,
        [weak_swing, strong_swing],
        min_distance=atr,
        min_strength=4,
        ob_candle_index=300,
    )

    assert result is not None
    assert result.price == pytest.approx(110.50), (
        f"Weak-strength swing must be rejected; expected 110.50, got {result.price}"
    )
    assert result.target_strength == 6


def test_all_swings_below_min_strength_returns_sentinel():
    """
    Case A variant — Every candidate is below min_strength; pool empties → sentinel.
    """
    entry = 111.0
    atr = 0.05

    swings = [
        sp(280, SwingType.LOW, 110.85, strength=2),
        sp(260, SwingType.LOW, 110.70, strength=3),
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        min_distance=atr, min_strength=4, ob_candle_index=300,
    )

    assert result is NO_VALID_TARGET


# ── Case C (explicit): Internal beats equal-strength external by scoring ──────

def test_internal_preferred_over_equal_strength_external_by_score():
    """
    Case C — Internal vs external.

    Two swings with identical strength (=4):
      * internal_swing at 4 ATRs  → dist_score = 1/4 = 0.250 → score = 1.00
      * external_swing at 10 ATRs → dist_score = (1/10)*0.5 = 0.050 → score = 0.20

    The internal swing must win.  This proves the >8-ATR half-weight penalty
    functions as intended: structurally equal targets are ordered by distance-range
    preference, not raw distance.
    """
    entry = 111.0
    atr = 0.10

    internal_swing = sp(200, SwingType.LOW, entry - 4 * atr, strength=4)   # 4 ATR → score=1.0
    external_swing = sp(100, SwingType.LOW, entry - 10 * atr, strength=4)  # 10 ATR → score=0.2

    result = find_irl_target(
        entry, Direction.BEARISH,
        [internal_swing, external_swing],
        min_distance=atr,
        ob_candle_index=300,
    )

    assert result is not None
    assert result.price == pytest.approx(entry - 4 * atr), (
        f"Internal (4-ATR) swing must outrank external (10-ATR) with equal strength; "
        f"got {result.price}"
    )


# ── Case D (extension): mitigated_swing_ids blocks even the best candidate ────

def test_mitigated_swing_excluded_via_blocked_ids():
    """
    Case D (blocked IDs extension).

    A swing in ``mitigated_swing_ids`` is unconditionally excluded.  The
    next-best unblocked candidate is selected instead.

    This mirrors what the causal pipeline does when a swing has been
    mitigated/consumed at a higher timeframe — the ID is blocklisted to prevent
    it re-entering the pool.
    """
    entry = 111.0
    atr = 0.05

    best_swing = sp(200, SwingType.LOW, 110.50, strength=8)   # would win, but mitigated
    next_swing = sp(180, SwingType.LOW, 110.20, strength=6)   # should be selected

    result = find_irl_target(
        entry, Direction.BEARISH,
        [best_swing, next_swing],
        mitigated_swing_ids={best_swing.id},
        min_distance=atr,
        ob_candle_index=300,
    )

    assert result is not None
    assert result.price == pytest.approx(110.20), (
        f"Mitigated best swing must be excluded; expected 110.20, got {result.price}"
    )
    assert result.swing_id == next_swing.id


# ── Case L: Target provenance — all metadata fields are correctly populated ───

def test_target_provenance_all_fields_populated():
    """
    Case L — Target provenance.

    The IRLTarget returned by find_irl_target must expose enough information for
    the UI and audit layer to explain the selection without reading the source code.

    Verified fields:
      * swing_id      — identifies the structural swing
      * direction     — bearish/bullish context
      * price         — the TP level
      * candle_index  — locates the swing in the M15 series
      * target_type   — "INTERNAL_SWING"
      * target_strength — structural significance
      * qualification_reason — embedded provenance: strength, dist_atrs, candidates
      * consumed      — must be False (consumed swings are hard-rejected)
    """
    entry = 111.0
    atr = 0.05

    target_swing = sp(200, SwingType.LOW, 110.60, strength=6)

    result = find_irl_target(
        entry, Direction.BEARISH, [target_swing],
        min_distance=atr, ob_candle_index=300,
    )

    assert result is not None

    # Identity
    assert result.swing_id == target_swing.id
    assert result.direction is Direction.BEARISH
    assert result.price == pytest.approx(110.60)
    assert result.candle_index == target_swing.index

    # Classification
    assert result.target_type == "INTERNAL_SWING"
    assert result.target_strength == 6
    assert result.consumed is False

    # Provenance string must be parseable
    reason = result.qualification_reason
    assert "strength=" in reason, f"qualification_reason missing strength: {reason!r}"
    assert "dist_atrs=" in reason, f"qualification_reason missing dist_atrs: {reason!r}"
    assert "candidates=" in reason, f"qualification_reason missing candidates: {reason!r}"

    # Values in provenance must be self-consistent
    dist_atrs = float(reason.split("dist_atrs=")[1].split(",")[0])
    expected_dist = (entry - target_swing.price) / atr
    assert dist_atrs == pytest.approx(expected_dist, abs=0.15), (
        f"dist_atrs in provenance ({dist_atrs}) inconsistent with computed ({expected_dist:.2f})"
    )
    candidates = int(reason.split("candidates=")[1])
    assert candidates == 1


# ── Case J (extension): Determinism under insertion-order reversal ────────────

def test_determinism_independent_of_input_list_order():
    """
    Case J (order invariance).

    Reversing the order of the input swing list must not change which target is
    selected.  This proves the selection is governed by the scoring function,
    not list-position tie-breaking.
    """
    entry = 111.0
    atr = 0.05

    swings = [
        sp(200, SwingType.LOW, 110.60, strength=6),
        sp(180, SwingType.LOW, 110.40, strength=4),
        sp(160, SwingType.LOW, 110.20, strength=8),
    ]

    r_forward  = find_irl_target(entry, Direction.BEARISH, swings,       min_distance=atr, ob_candle_index=300)
    r_reversed = find_irl_target(entry, Direction.BEARISH, list(reversed(swings)), min_distance=atr, ob_candle_index=300)

    assert r_forward is not None
    assert r_forward.swing_id  == r_reversed.swing_id
    assert r_forward.price     == r_reversed.price
    assert r_forward.qualification_reason == r_reversed.qualification_reason


# ── min_rr R:R filter (new tests) ─────────────────────────────────────────────

def test_nearby_irl_rejected_when_stop_distance_enforces_min_rr():
    """
    When stop_distance + min_rr are set, a swing too close to generate 1:1 R:R
    must be hard-rejected.  A deeper swing that does meet the threshold is returned.

    Setup:
      entry = 1.32193 (GBPUSD-style short)
      stop  = 1.33106 → stop_distance = 0.00913 (91.3 pips)
      min_rr = 1.0     → target must be ≥ 0.00913 below entry (≤ 1.31280)

      near_swing  @ 1.31810 — distance 0.00383 (38 pips) — fails R:R (0.42)
      far_swing   @ 1.31200 — distance 0.00993 (99 pips) — passes R:R (1.09)
    """
    entry = 1.32193
    stop_distance = 0.00913  # stop at 1.33106
    atr = 0.00050            # ~5 pip ATR on M15

    near_swing = sp(200, SwingType.LOW, 1.31810, strength=6)
    far_swing  = sp(100, SwingType.LOW, 1.31200, strength=6)

    result = find_irl_target(
        entry, Direction.BEARISH,
        [near_swing, far_swing],
        min_distance=atr,
        ob_candle_index=300,
        stop_distance=stop_distance,
        min_rr=1.0,
    )

    assert result is not None, "far_swing at 1.31200 should qualify (R:R 1.09)"
    assert result.price == pytest.approx(1.31200), (
        f"near_swing at 1.31810 (R:R 0.42) must be rejected; expected 1.31200, got {result.price}"
    )


def test_no_irl_qualifies_when_all_swings_below_min_rr():
    """
    When every candidate swing is too close to satisfy min_rr × stop_distance,
    find_irl_target must return NO_VALID_TARGET rather than accepting a poor-R:R trade.
    """
    entry = 1.32193
    stop_distance = 0.00913
    atr = 0.00050

    swings = [
        sp(200, SwingType.LOW, 1.31810, strength=6),  # 38 pips — R:R 0.42
        sp(180, SwingType.LOW, 1.31900, strength=8),  # 29 pips — R:R 0.32
    ]

    result = find_irl_target(
        entry, Direction.BEARISH, swings,
        min_distance=atr,
        ob_candle_index=300,
        stop_distance=stop_distance,
        min_rr=1.0,
    )

    assert result is NO_VALID_TARGET, (
        "All swings fail min R:R 1.0 — must return NO_VALID_TARGET, not a bad trade"
    )


def test_min_rr_zero_does_not_filter_any_swing():
    """
    When min_rr=0.0 (default), the R:R filter is disabled and the scoring-best
    candidate is returned regardless of how poor the R:R would be.
    """
    entry = 1.32193
    atr = 0.00050

    near_swing = sp(200, SwingType.LOW, 1.31810, strength=8)  # R:R 0.42 but wins on score

    result = find_irl_target(
        entry, Direction.BEARISH, [near_swing],
        min_distance=atr,
        ob_candle_index=300,
        stop_distance=0.00913,
        min_rr=0.0,             # disabled
    )

    assert result is not None, "With min_rr=0, near_swing must qualify"
    assert result.price == pytest.approx(1.31810)
