
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from .lifecycle import SetupLifecycle, SetupState
from .models import Direction
from .risk import RiskEngine
from .setup import TradeSetup


class ReplayOrderState(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    STOPPED = "STOPPED"
    TARGETED = "TARGETED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class ReplayFill:
    index: int
    price: float


@dataclass(frozen=True)
class ReplayResult:
    setup_id: str
    order_state: ReplayOrderState
    entry: Optional[float]
    exit: Optional[float]
    exit_index: Optional[int]
    pnl_price: float
    bars_observed: int


class ReplayBroker:
    """Deterministic candle-level pending-limit simulator.

    This intentionally models conservative OHLC ambiguity: if a single candle
    touches both entry and SL/TP, the stop is resolved first. This avoids
    optimistic fills and can be replaced by tick replay later.
    """

    def __init__(self, candles: pd.DataFrame):
        self.candles = candles.reset_index(drop=True)

    def run(self, setup: TradeSetup) -> ReplayResult:
        pending = True
        filled_at: Optional[float] = None
        exit_at: Optional[float] = None
        exit_index: Optional[int] = None
        state = ReplayOrderState.PENDING

        for i, row in self.candles.iterrows():
            high, low = float(row["high"]), float(row["low"])

            if pending:
                if setup.direction is Direction.BULLISH:
                    if low <= setup.invalidation_level:
                        state = ReplayOrderState.INVALIDATED
                        break
                    if low <= setup.entry <= high:
                        filled_at = setup.entry
                        pending = False
                        state = ReplayOrderState.FILLED
                else:
                    if high >= setup.invalidation_level:
                        state = ReplayOrderState.INVALIDATED
                        break
                    if low <= setup.entry <= high:
                        filled_at = setup.entry
                        pending = False
                        state = ReplayOrderState.FILLED
                continue

            if setup.direction is Direction.BULLISH:
                if low <= setup.stop_loss:
                    exit_at, exit_index, state = setup.stop_loss, i, ReplayOrderState.STOPPED
                    break
                if high >= setup.take_profit:
                    exit_at, exit_index, state = setup.take_profit, i, ReplayOrderState.TARGETED
                    break
            else:
                if high >= setup.stop_loss:
                    exit_at, exit_index, state = setup.stop_loss, i, ReplayOrderState.STOPPED
                    break
                if low <= setup.take_profit:
                    exit_at, exit_index, state = setup.take_profit, i, ReplayOrderState.TARGETED
                    break

        pnl = 0.0 if filled_at is None or exit_at is None else (
            exit_at - filled_at if setup.direction is Direction.BULLISH
            else filled_at - exit_at
        )
        return ReplayResult(
            setup.id, state, filled_at, exit_at, exit_index, pnl, len(self.candles)
        )


@dataclass(frozen=True)
class ReplayMetrics:
    setups: int
    filled: int
    targets: int
    stops: int
    invalidated: int
    pending: int
    gross_price_pnl: float


def summarize(results: list[ReplayResult]) -> ReplayMetrics:
    return ReplayMetrics(
        setups=len(results),
        filled=sum(r.order_state in {ReplayOrderState.FILLED, ReplayOrderState.STOPPED, ReplayOrderState.TARGETED} for r in results),
        targets=sum(r.order_state is ReplayOrderState.TARGETED for r in results),
        stops=sum(r.order_state is ReplayOrderState.STOPPED for r in results),
        invalidated=sum(r.order_state is ReplayOrderState.INVALIDATED for r in results),
        pending=sum(r.order_state is ReplayOrderState.PENDING for r in results),
        gross_price_pnl=sum(r.pnl_price for r in results),
    )
