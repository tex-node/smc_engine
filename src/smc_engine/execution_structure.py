from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .models import Direction, Inducement, POI

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
class ExecutionContext:
    poi: POI
    order_block: OrderBlock
    inducement: Inducement
    direction: Direction

def _opposite_candle(df: pd.DataFrame, displacement_index: int, direction: Direction, search_back: int) -> Optional[int]:
    start = max(0, displacement_index - search_back)
    for i in range(displacement_index - 1, start - 1, -1):
        row = df.iloc[i]
        if direction is Direction.BULLISH and row['close'] < row['open']:
            return i
        if direction is Direction.BEARISH and row['close'] > row['open']:
            return i
    return None

def find_order_blocks(df: pd.DataFrame, displacement_indices: list[int], timeframe: str = 'M15', search_back: int = 5) -> list[OrderBlock]:
    blocks: list[OrderBlock] = []
    for d in displacement_indices:
        if d <= 0 or d >= len(df):
            continue
        direction = Direction.BULLISH if df.iloc[d]['close'] > df.iloc[d]['open'] else Direction.BEARISH
        source = _opposite_candle(df, d, direction, search_back)
        if source is None:
            continue
        row = df.iloc[source]
        blocks.append(OrderBlock(
            id=f'OB-{timeframe}-{source}-{direction.value}', direction=direction, timeframe=timeframe,
            candle_index=source, candle_time=row['time'], low=float(row['low']), high=float(row['high']),
            mitigation_price=float(row['high'] if direction is Direction.BULLISH else row['low']),
            source_displacement_index=d,
        ))
    return blocks

def find_inducements(df: pd.DataFrame, order_blocks: list[OrderBlock], swings, max_bars_after_ob: int = 8, as_of=None) -> list[Inducement]:
    cutoff = None if as_of is None else pd.Timestamp(as_of)
    if cutoff is not None:
        cutoff = cutoff.tz_localize('UTC') if cutoff.tzinfo is None else cutoff.tz_convert('UTC')
    result: list[Inducement] = []
    for ob in order_blocks:
        eligible = []
        for s in swings:
            if not (ob.candle_index < s.index <= ob.source_displacement_index + max_bars_after_ob):
                continue
            confirmation_time = getattr(s, 'confirmation_time', s.time)
            if cutoff is not None:
                confirmed = pd.Timestamp(confirmation_time)
                confirmed = confirmed.tz_localize('UTC') if confirmed.tzinfo is None else confirmed.tz_convert('UTC')
                if confirmed > cutoff:
                    continue
            eligible.append(s)
        if ob.direction is Direction.BULLISH:
            eligible = [s for s in eligible if getattr(s.type, 'value', s.type) == 'LOW']
        else:
            eligible = [s for s in eligible if getattr(s.type, 'value', s.type) == 'HIGH']
        if not eligible:
            continue
        swing = min(eligible, key=lambda s: (pd.Timestamp(getattr(s, "confirmation_time", s.time)), s.index))
        result.append(Inducement(
            id=f'IDM-{ob.id}-{swing.index}', direction=ob.direction, candle_index=swing.index,
            candle_time=swing.time, level=swing.price, source_swing_index=swing.index,
            order_block_id=ob.id, confirmation_time=getattr(swing, 'confirmation_time', swing.time),
        ))
    return result

def execution_context(poi: POI, order_blocks: list[OrderBlock], inducements: list[Inducement], direction: Direction) -> list[ExecutionContext]:
    if poi.direction is not direction:
        return []
    idm_by_ob = {x.order_block_id: x for x in inducements}
    return [ExecutionContext(poi, ob, idm_by_ob[ob.id], direction) for ob in order_blocks if ob.direction is direction and ob.valid and ob.id in idm_by_ob]