from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .execution_structure import ExecutionContext, _ts_key
from .models import Direction, LiquiditySweep, SetupState, StructureEvent, SwingPoint, SwingType


@dataclass(frozen=True)
class IRLTarget:
    swing_id: str
    direction: Direction
    price: float
    candle_index: int
    candle_time: object
    confirmation_time: object
    target_type: str = "INTERNAL_SWING"
    target_strength: int = 0
    qualification_reason: str = ""
    consumed: bool = False


# Sentinel: find_irl_target returns this when no meaningful target qualifies.
NO_VALID_TARGET: Optional[IRLTarget] = None


def _as_of_row(ohlc: pd.DataFrame, as_of) -> int:
    """Latest integer row index in ohlc at or before as_of."""
    times = pd.to_datetime(ohlc["time"], utc=True)
    ts = pd.Timestamp(as_of)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    mask = times <= ts
    return int(mask[mask].index[-1]) if mask.any() else len(ohlc) - 1


def _swing_consumed(
    swing: SwingPoint,
    direction: Direction,
    ohlc: pd.DataFrame,
    as_of_idx: int,
) -> bool:
    """True if candles after the swing's confirmation and before as_of_idx
    traded through (or to) the swing's price level — meaning the liquidity
    pool has already been collected."""
    start = swing.confirmation_index + 1
    if start > as_of_idx:
        return False
    window = ohlc.iloc[start:as_of_idx + 1]
    if window.empty:
        return False
    if direction is Direction.BEARISH:
        # TP is a swing LOW: consumed if any subsequent low reached that level
        return bool((window["low"] <= swing.price).any())
    return bool((window["high"] >= swing.price).any())


def _irl_score(swing: SwingPoint, entry: float, direction: Direction, atr: float) -> float:
    """Score a qualified IRL candidate; higher is better.

    Combines structural strength with a distance factor that favours
    internal-range targets (1–8 ATRs from entry) over OB-adjacent
    noise (< 1 ATR, already hard-rejected) and external extremes (> 8 ATRs).
    """
    dist = (entry - swing.price) if direction is Direction.BEARISH else (swing.price - entry)
    dist_atrs = dist / atr if atr > 0 else dist
    if dist_atrs <= 0:
        return -1.0
    dist_score = 1.0 / dist_atrs if dist_atrs <= 8.0 else (1.0 / dist_atrs) * 0.5
    return swing.strength * dist_score


def find_irl_target(
    entry: float,
    direction: Direction,
    swings: list[SwingPoint],
    mitigated_swing_ids: Optional[set[str]] = None,
    as_of: object = None,
    exclude_swing_ids: Optional[set[str]] = None,
    min_distance: float = 0.0,
    ohlc: Optional[pd.DataFrame] = None,
    min_strength: int = 0,
    ob_candle_index: Optional[int] = None,
    ob_proximity_bars: int = 10,
    stop_distance: float = 0.0,
    min_rr: float = 0.0,
) -> Optional[IRLTarget]:
    """Return the highest-quality meaningful internal-range liquidity target.

    Hard rejects (in order):
      - Wrong side of entry or < min_distance from entry (ATR-based threshold)
      - In mitigated_swing_ids or exclude_swing_ids
      - Structural strength below min_strength
      - Candle index within ob_proximity_bars of ob_candle_index (OB-adjacent noise)
      - Level already consumed by subsequent candles before as_of (if ohlc supplied)
      - Reward distance < stop_distance * min_rr (R:R below minimum threshold)

    Survivors are scored by ``strength × distance_factor`` where distance_factor
    weights internal-range targets (1–8 ATRs) over external extremes.  The
    highest scorer is returned.  Returns NO_VALID_TARGET (None) when nothing
    qualifies, rather than manufacturing a TP.
    """
    blocked = (mitigated_swing_ids or set()) | (exclude_swing_ids or set())

    pool = (
        [s for s in swings if pd.Timestamp(s.confirmation_time) <= pd.Timestamp(as_of)]
        if as_of is not None else list(swings)
    )

    as_of_idx: Optional[int] = None
    if ohlc is not None:
        as_of_idx = _as_of_row(ohlc, as_of) if as_of is not None else len(ohlc) - 1

    qualified: list[SwingPoint] = []
    for s in pool:
        if s.id in blocked:
            continue
        dist = (entry - s.price) if direction is Direction.BEARISH else (s.price - entry)
        if direction is Direction.BEARISH and s.type is not SwingType.LOW:
            continue
        if direction is Direction.BULLISH and s.type is not SwingType.HIGH:
            continue
        if dist <= min_distance:
            continue
        if stop_distance > 0 and min_rr > 0 and dist < stop_distance * min_rr:
            continue
        if s.strength < min_strength:
            continue
        if ob_candle_index is not None and abs(s.index - ob_candle_index) <= ob_proximity_bars:
            continue
        if ohlc is not None and as_of_idx is not None:
            if _swing_consumed(s, direction, ohlc, as_of_idx):
                continue
        qualified.append(s)

    if not qualified:
        return NO_VALID_TARGET

    atr = min_distance if min_distance > 0 else 1.0
    best = max(qualified, key=lambda s: _irl_score(s, entry, direction, atr))

    dist = (entry - best.price) if direction is Direction.BEARISH else (best.price - entry)
    dist_atrs = dist / atr
    reason = f"strength={best.strength},dist_atrs={dist_atrs:.1f},candidates={len(qualified)}"

    return IRLTarget(
        best.id, direction, best.price, best.index, best.time, best.confirmation_time,
        "INTERNAL_SWING", best.strength, reason, False,
    )


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
    irl_target_type: str = "INTERNAL_SWING"
    irl_target_strength: int = 0
    irl_qualification_reason: str = ""

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
        id=f"SETUP-{symbol}-{_ts_key(csd.candle_time)}-{context.order_block.id}", symbol=symbol,
        direction=context.direction, created_time=created_time if created_time is not None else csd.candle_time,
        poi_id=context.poi.id, sweep_id=sweep.id, csd_id=csd.id, protected_level=protected,
        order_block_id=context.order_block.id, inducement_id=context.inducement.id, entry=entry,
        stop_loss=stop, take_profit=irl.price, irl_swing_id=irl.swing_id,
        invalidation_level=invalidation, risk_percent=risk_percent,
        irl_target_type=irl.target_type,
        irl_target_strength=irl.target_strength,
        irl_qualification_reason=irl.qualification_reason,
    )


@dataclass(frozen=True)
class SetupLifecycle:
    state: SetupState
    event_index: Optional[int]
    event_time: object
    reason: str


def evaluate_setup_lifecycle(setup: TradeSetup, df: pd.DataFrame, current_index: Optional[int] = None, expiry_bars: int = 20) -> SetupLifecycle:
    """Evaluate setup state using only candles strictly after setup creation.

    The candle stream must be strictly chronological. Rejecting duplicate or
    out-of-order timestamps prevents row-order-dependent lifecycle results.
    """
    required = {"time", "high", "low"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    times = pd.to_datetime(df["time"], utc=True)
    if times.duplicated().any():
        raise ValueError("Candle timestamps must be unique")
    if not times.is_monotonic_increasing:
        raise ValueError("Candle timestamps must be strictly chronological")
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


def resolve_setup_conflicts(
    setups: list[TradeSetup],
    lifecycles: dict[str, SetupLifecycle],
) -> list[TradeSetup]:
    """Return a deterministic execution set for overlapping triggered setups.

    Setups on different symbols or different trigger candles may coexist. When
    multiple setups for the same symbol trigger on the same candle, only the
    highest-priority setup is executable. Priority is stable and independent of
    dataframe/list order: earliest creation time, then higher risk/reward, then
    lexical setup id. Non-triggered setups are returned unchanged.
    """
    triggered: list[TradeSetup] = []
    other: list[TradeSetup] = []
    for setup in setups:
        lifecycle = lifecycles.get(setup.id)
        if lifecycle is not None and lifecycle.state is SetupState.TRIGGERED and lifecycle.event_index is not None:
            triggered.append(setup)
        else:
            other.append(setup)

    groups: dict[tuple[str, int], list[TradeSetup]] = {}
    for setup in triggered:
        lifecycle = lifecycles[setup.id]
        groups.setdefault((setup.symbol, lifecycle.event_index), []).append(setup)

    winners: list[TradeSetup] = []
    for group in groups.values():
        winners.append(min(
            group,
            key=lambda s: (
                pd.Timestamp(s.created_time).value,
                -s.risk_reward,
                s.id,
            ),
        ))
    return sorted(other + winners, key=lambda s: (pd.Timestamp(s.created_time).value, s.id))
