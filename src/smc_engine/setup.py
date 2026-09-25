from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .execution_structure import ExecutionContext
from .models import Direction, LiquiditySweep, StructureEvent, SwingPoint, SwingType


@dataclass(frozen=True)
class IRLTarget:
    swing_id: str
    direction: Direction
    price: float
    candle_index: int
    candle_time: object


def find_irl_target(
    entry: float,
    direction: Direction,
    swings: list[SwingPoint],
    mitigated_swing_ids: Optional[set[str]] = None,
    as_of: object = None,
) -> Optional[IRLTarget]:
    """Return the first unmitigated structural liquidity point available as of as_of."""
    blocked = mitigated_swing_ids or set()
    if as_of is not None:
        swings = [
            s for s in swings
            if pd.Timestamp(s.confirmation_time) <= pd.Timestamp(as_of)
        ]
    if direction is Direction.BULLISH:
        candidates = [
            s for s in swings
            if s.type is SwingType.HIGH and s.price > entry and s.id not in blocked
        ]
        candidates.sort(key=lambda s: (s.price, s.index))
    else:
        candidates = [
            s for s in swings
            if s.type is SwingType.LOW and s.price < entry and s.id not in blocked
        ]
        candidates.sort(key=lambda s: (-s.price, s.index))
    if not candidates:
        return None
    s = candidates[0]
    return IRLTarget(s.id, direction, s.price, s.index, s.time, s.confirmation_time)


@dataclass(frozen=True)
class TradeSetup:
    id: str
    symbol: str
    direction: Direction
    created_time: object
    poi_id: str
    sweep_id: str
    csd_id: str
    protected_level: float
    order_block_id: str
    inducement_id: str
    entry: float
    stop_loss: float
    take_profit: float
    irl_swing_id: str
    invalidation_level: float
    risk_percent: float

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def risk_reward(self) -> float:
        if self.risk_distance <= 0:
            return 0.0
        return self.reward_distance / self.risk_distance


def protected_level_from_sweep(sweep: LiquiditySweep) -> float:
    return sweep.sweep_extreme


def build_trade_setup(
    symbol: str,
    context: ExecutionContext,
    sweep: LiquiditySweep,
    csd: StructureEvent,
    irl: IRLTarget,
    risk_percent: float = 1.0,
    stop_buffer: float = 0.0,
    created_time: object = None,
) -> TradeSetup:
    if csd.type.value != "CSD":
        raise ValueError("Trade setup requires a CSD event")

    protected = protected_level_from_sweep(sweep)
    if context.direction is Direction.BULLISH:
        stop = protected - stop_buffer
        entry = context.order_block.mitigation_price
        invalidation = protected
    else:
        stop = protected + stop_buffer
        entry = context.order_block.mitigation_price
        invalidation = protected

    if context.direction is Direction.BULLISH and not (stop < entry < irl.price):
        raise ValueError("Invalid bullish setup geometry")
    if context.direction is Direction.BEARISH and not (irl.price < entry < stop):
        raise ValueError("Invalid bearish setup geometry")

    return TradeSetup(
        id=f"SETUP-{symbol}-{csd.candle_index}-{context.order_block.id}",
        symbol=symbol,
        direction=context.direction,
        created_time=created_time if created_time is not None else csd.candle_time,
        poi_id=context.poi.id,
        sweep_id=sweep.id,
        csd_id=csd.id,
        protected_level=protected,
        order_block_id=context.order_block.id,
        inducement_id=context.inducement.id,
        entry=entry,
        stop_loss=stop,
        take_profit=irl.price,
        irl_swing_id=irl.swing_id,
        invalidation_level=invalidation,
        risk_percent=risk_percent,
    )
