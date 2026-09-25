
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .models import Direction, POI


@dataclass(frozen=True)
class OrderBlock:
    id: str
    direction: Direction
    timeframe: str
    candle_index: int
    candle_time: object
    low: float
    high: float
    mitigation_price: float
    source_displacement_index: int
    valid: bool = True

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


@dataclass(frozen=True)
class Inducement:
    id: str
    direction: Direction
    candle_index: int
    candle_time: object
    level: float
    source_swing_index: int
    order_block_id: str


@dataclass(frozen=True)
class ExecutionContext:
    poi: POI
    order_block: OrderBlock
    inducement: Inducement
    direction: Direction


def _opposite_candle(df: pd.DataFrame, displacement_index: int, direction: Direction, search_back: int) -> Optional[int]:
    start = max(0, displacement_index - search_back)
    for i in range(displacement_index - 1, start - 1, -1):
        row = df.iloc[i]
        if direction is Direction.BULLISH and row["close"] < row["open"]:
            return i
        if direction is Direction.BEARISH and row["close"] > row["open"]:
            return i
    return None


def find_order_blocks(
    df: pd.DataFrame,
    displacement_indices: list[int],
    timeframe: str = "M15",
    search_back: int = 5,
) -> list[OrderBlock]:
    """Find the last opposite candle preceding each directional displacement.

    The OB zone is the full high/low range of that candle. Entry is its
    directional mitigation boundary: bullish OB -> high, bearish OB -> low.
    """
    blocks: list[OrderBlock] = []
    for d in displacement_indices:
        if d <= 0 or d >= len(df):
            continue
        direction = (
            Direction.BULLISH
            if df.iloc[d]["close"] > df.iloc[d]["open"]
            else Direction.BEARISH
        )
        source = _opposite_candle(df, d, direction, search_back)
        if source is None:
            continue
        row = df.iloc[source]
        blocks.append(
            OrderBlock(
                id=f"OB-{timeframe}-{source}-{direction.value}",
                direction=direction,
                timeframe=timeframe,
                candle_index=source,
                candle_time=row["time"],
                low=float(row["low"]),
                high=float(row["high"]),
                mitigation_price=float(row["high"] if direction is Direction.BULLISH else row["low"]),
                source_displacement_index=d,
            )
        )
    return blocks


def find_inducements(
    df: pd.DataFrame,
    order_blocks: list[OrderBlock],
    swings,
    max_bars_after_ob: int = 8,
) -> list[Inducement]:
    """Find a confirmed local swing between an OB and subsequent displacement.

    For a bullish setup, a sell-side inducement is a local swing low that is
    formed after the OB and before the execution sequence. For bearish setups,
    the equivalent is a local swing high. Only swings after the OB are eligible.
    """
    result: list[Inducement] = []
    for ob in order_blocks:
        eligible = [
            s for s in swings
            if ob.candle_index < s.index <= ob.source_displacement_index + max_bars_after_ob
        ]
        if ob.direction is Direction.BULLISH:
            eligible = [s for s in eligible if getattr(s.type, "value", s.type) == "LOW"]
        else:
            eligible = [s for s in eligible if getattr(s.type, "value", s.type) == "HIGH"]
        if not eligible:
            continue
        swing = eligible[-1]
        result.append(
            Inducement(
                id=f"IDM-{ob.id}-{swing.index}",
                direction=ob.direction,
                candle_index=swing.index,
                candle_time=swing.time,
                level=swing.price,
                source_swing_index=swing.index,
                order_block_id=ob.id,
            )
        )
    return result


def execution_context(
    poi: POI,
    order_blocks: list[OrderBlock],
    inducements: list[Inducement],
    direction: Direction,
) -> list[ExecutionContext]:
    """Join valid M15 OBs and IDM structures to an active D1 POI."""
    if poi.direction is not direction:
        return []
    idm_by_ob = {x.order_block_id: x for x in inducements}
    return [
        ExecutionContext(poi, ob, idm_by_ob[ob.id], direction)
        for ob in order_blocks
        if ob.direction is direction and ob.valid and ob.id in idm_by_ob
    ]
