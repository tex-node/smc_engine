from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .execution_structure import ExecutionContext
from .models import Direction, LiquiditySweep, SetupState, StructureEvent, SwingPoint, SwingType


@dataclass(frozen=True)
class IRLTarget:
    swing_id: str
    direction: Direction
    price: float
    candle_index: int
    candle_time: object
    confirmation_time: object


def find_irl_target(entry: float, direction: Direction, swings: list[SwingPoint], mitigated_swing_ids: Optional[set[str]] = None, as_of: object = None) -> Optional[IRLTarget]:
    """Return the first unmitigated structural liquidity point available as of as_of."""
    blocked = mitigated_swing_ids or set()
    if as_of is not None:
        swings = [s for s in swings if pd.Timestamp(s.confirmation_time) <= pd.Timestamp(as_of)]
    if direction is Direction.BULLISH:
        candidates = [s for s in swings if s.type is SwingType.HIGH and s.price > entry and s.id not in blocked]
        candidates.sort(key=lambda s: (s.price, s.index))
    else:
        candidates = [s for s in swings if s.type is SwingType.LOW and s.price < entry and s.id not in blocked]
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


def build_trade_setup(symbol: str, context: ExecutionContext, sweep: LiquiditySweep, csd: StructureEvent, irl: IRLTarget, risk_percent: float = 1.0, stop_buffer: float = 0.0, created_time: object = None) -> TradeSetup:
    if csd.type.value != "CSD":
        raise ValueError("Trade setup requires a CSD event")
    protected = protected_level_from_sweep(sweep)
    if context.direction is Direction.BULLISH:
        stop, entry, invalidation = protected - stop_buffer, context.order_block.mitigation_price, protected
    else:
        stop, entry, invalidation = protected + stop_buffer, context.order_block.mitigation_price, protected
    if context.direction is Direction.BULLISH and not (stop < entry < irl.price):
        raise ValueError("Invalid bullish setup geometry")
    if context.direction is Direction.BEARISH and not (irl.price < entry < stop):
        raise ValueError("Invalid bearish setup geometry")
    return TradeSetup(
        id=f"SETUP-{symbol}-{csd.candle_index}-{context.order_block.id}", symbol=symbol,
        direction=context.direction, created_time=created_time if created_time is not None else csd.candle_time,
        poi_id=context.poi.id, sweep_id=sweep.id, csd_id=csd.id, protected_level=protected,
        order_block_id=context.order_block.id, inducement_id=context.inducement.id, entry=entry,
        stop_loss=stop, take_profit=irl.price, irl_swing_id=irl.swing_id,
        invalidation_level=invalidation, risk_percent=risk_percent,
    )


@dataclass(frozen=True)
class SetupLifecycle:
    state: SetupState
    event_index: Optional[int]
    event_time: object
    reason: str


def evaluate_setup_lifecycle(setup: TradeSetup, df: pd.DataFrame, current_index: Optional[int] = None, expiry_bars: int = 20) -> SetupLifecycle:
    """Evaluate setup state using only candles strictly after setup creation.

    Pending setups become TRIGGERED on entry touch, INVALIDATED on protected-level
    breach, or EXPIRED after ``expiry_bars`` eligible candles. Triggered setups
    become FILLED at target or INVALIDATED at stop. If a candle touches both
    terminal levels, the result is AMBIGUOUS rather than assuming intrabar order.
    """
    required = {"time", "high", "low"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    times = pd.to_datetime(df["time"], utc=True)
    created = pd.Timestamp(setup.created_time)
    if created.tzinfo is None:
        created = created.tz_localize("UTC")
    else:
        created = created.tz_convert("UTC")
    eligible = [i for i, t in enumerate(times) if t > created]
    if current_index is not None:
        eligible = [i for i in eligible if i <= current_index]
    state = SetupState.PENDING
    seen = 0
    for i in eligible:
        row = df.iloc[i]
        high, low = float(row["high"]), float(row["low"])
        if state is SetupState.PENDING:
            seen += 1
            if setup.direction is Direction.BULLISH:
                invalidated = low <= setup.invalidation_level
                triggered = low <= setup.entry <= high
                expired_target = high >= setup.take_profit
            else:
                invalidated = high >= setup.invalidation_level
                triggered = low <= setup.entry <= high
                expired_target = low <= setup.take_profit
            if invalidated and triggered:
                return SetupLifecycle(SetupState.AMBIGUOUS, i, row["time"], "entry_and_invalidation_touched_same_candle")
            if invalidated:
                return SetupLifecycle(SetupState.INVALIDATED, i, row["time"], "protected_level_breached_before_entry")
            if expired_target:
                return SetupLifecycle(SetupState.EXPIRED, i, row["time"], "target_reached_before_entry")
            if triggered:
                state = SetupState.TRIGGERED
                continue
            if seen >= expiry_bars:
                return SetupLifecycle(SetupState.EXPIRED, i, row["time"], "pending_entry_expiry")
        else:
            if setup.direction is Direction.BULLISH:
                stopped = low <= setup.stop_loss
                filled = high >= setup.take_profit
            else:
                stopped = high >= setup.stop_loss
                filled = low <= setup.take_profit
            if stopped and filled:
                return SetupLifecycle(SetupState.AMBIGUOUS, i, row["time"], "stop_and_target_touched_same_candle")
            if stopped:
                return SetupLifecycle(SetupState.INVALIDATED, i, row["time"], "stop_loss_hit")
            if filled:
                return SetupLifecycle(SetupState.FILLED, i, row["time"], "take_profit_hit")
    return SetupLifecycle(state, None, None, "still_pending" if state is SetupState.PENDING else "position_open")
