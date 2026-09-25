
from __future__ import annotations
from typing import Optional
import pandas as pd
from .models import (
    Direction, LiquidityPool, LiquiditySide, LiquiditySweep,
    StructureEvent, StructureEventType, SwingPoint, SwingType,
)

def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[SwingPoint]:
    """Find confirmed pivot swings using completed candles only."""
    if left < 1 or right < 1:
        raise ValueError("left and right must be >= 1")
    missing = {"time", "high", "low"} - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    swings: list[SwingPoint] = []
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    for i in range(left, len(df) - right):
        h, l = highs[i], lows[i]
        if h > max(highs[i-left:i]) and h >= max(highs[i+1:i+right+1]):
            swings.append(SwingPoint(f"SH-{i}", i, df.iloc[i]["time"], SwingType.HIGH, float(h), left + right))
        if l < min(lows[i-left:i]) and l <= min(lows[i+1:i+right+1]):
            swings.append(SwingPoint(f"SL-{i}", i, df.iloc[i]["time"], SwingType.LOW, float(l), left + right))
    return swings

def build_liquidity_pools(swings: list[SwingPoint]) -> list[LiquidityPool]:
    pools: list[LiquidityPool] = []
    for s in swings:
        side = LiquiditySide.BUY_SIDE if s.type is SwingType.HIGH else LiquiditySide.SELL_SIDE
        pools.append(LiquidityPool(f"LQ-{s.id}", side, s.price, s.id, s.time))
    return pools

def detect_sweeps(df: pd.DataFrame, liquidity: list[LiquidityPool], lookback_bars: int = 20) -> list[LiquiditySweep]:
    """Detect wick-through and close-back sweeps of confirmed swings."""
    sweeps: list[LiquiditySweep] = []
    for pool in liquidity:
        source_idx = int(pool.source_swing_id.split("-")[-1])
        start, end = source_idx + 1, min(len(df), source_idx + 1 + lookback_bars)
        for i in range(start, end):
            row = df.iloc[i]
            if pool.side is LiquiditySide.SELL_SIDE and row["low"] < pool.price and row["close"] > pool.price:
                sweeps.append(LiquiditySweep(
                    f"SWEEP-{i}-{pool.id}", pool.side, pool.price, float(row["low"]),
                    pool.id, i, row["time"], float(row["close"])
                ))
                break
            if pool.side is LiquiditySide.BUY_SIDE and row["high"] > pool.price and row["close"] < pool.price:
                sweeps.append(LiquiditySweep(
                    f"SWEEP-{i}-{pool.id}", pool.side, pool.price, float(row["high"]),
                    pool.id, i, row["time"], float(row["close"])
                ))
                break
    return sorted(sweeps, key=lambda x: x.candle_index)

def detect_structure_breaks(df: pd.DataFrame, swings: list[SwingPoint], start_index: int = 0) -> list[StructureEvent]:
    """Detect closes beyond the most recent confirmed swing level."""
    highs = [s for s in swings if s.type is SwingType.HIGH]
    lows = [s for s in swings if s.type is SwingType.LOW]
    events: list[StructureEvent] = []
    for i in range(max(start_index, 1), len(df)):
        row = df.iloc[i]
        prior_highs = [s for s in highs if s.index < i]
        prior_lows = [s for s in lows if s.index < i]
        if prior_highs and row["close"] > prior_highs[-1].price:
            h = prior_highs[-1]
            events.append(StructureEvent(f"BOS-H-{i}", StructureEventType.BOS, Direction.BULLISH, h.price, i, row["time"], h.id))
        if prior_lows and row["close"] < prior_lows[-1].price:
            l = prior_lows[-1]
            events.append(StructureEvent(f"BOS-L-{i}", StructureEventType.BOS, Direction.BEARISH, l.price, i, row["time"], l.id))
    return events

def confirm_csd(
    df: pd.DataFrame,
    sweep: LiquiditySweep,
    breaks: list[StructureEvent],
    max_bars_after_sweep: int = 6,
) -> Optional[StructureEvent]:
    """Confirm a directional structure break within a bounded window after a sweep."""
    desired = Direction.BULLISH if sweep.side is LiquiditySide.SELL_SIDE else Direction.BEARISH
    candidates = [
        b for b in breaks
        if b.direction is desired
        and sweep.candle_index < b.candle_index <= sweep.candle_index + max_bars_after_sweep
    ]
    if not candidates:
        return None
    b = candidates[0]
    return StructureEvent(
        f"CSD-{sweep.id}", StructureEventType.CSD, b.direction, b.level,
        b.candle_index, b.candle_time, b.source_swing_id, sweep.id
    )
