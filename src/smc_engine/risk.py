
from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Any, Optional

from .market import SymbolSpec
from .models import Direction
from .setup import TradeSetup


@dataclass(frozen=True)
class RiskQuote:
    volume: float
    estimated_loss: float
    requested_risk: float
    risk_percent: float


@dataclass(frozen=True)
class ExecutionConstraints:
    min_stop_distance: float
    min_volume: float
    max_volume: float
    volume_step: float
    tick_size: float


class RiskEngine:
    """Broker-aware sizing and preflight validation.

    This layer never silently increases requested risk to satisfy a broker's
    minimum volume. If the minimum volume is too large, the trade is rejected.
    """

    def __init__(self, spec: SymbolSpec):
        self.spec = spec

    def constraints(self) -> ExecutionConstraints:
        stop_level = max(self.spec.trade_stops_level, self.spec.trade_freeze_level)
        return ExecutionConstraints(
            min_stop_distance=stop_level * self.spec.point,
            min_volume=self.spec.volume_min,
            max_volume=self.spec.volume_max,
            volume_step=self.spec.volume_step,
            tick_size=self.spec.tick_size,
        )

    def volume_for_risk(
        self,
        balance: float,
        risk_percent: float,
        entry: float,
        stop_loss: float,
    ) -> RiskQuote:
        if balance <= 0 or risk_percent <= 0:
            raise ValueError("balance and risk_percent must be positive")
        if entry == stop_loss:
            raise ValueError("entry and stop_loss cannot be equal")
        if self.spec.tick_size <= 0 or self.spec.tick_value <= 0:
            raise ValueError("broker tick size/value must be positive")
        risk_money = balance * risk_percent / 100.0
        loss_per_lot = abs(entry - stop_loss) / self.spec.tick_size * self.spec.tick_value
        raw = risk_money / loss_per_lot
        volume = floor(raw / self.spec.volume_step) * self.spec.volume_step
        volume = round(volume, self._volume_digits())
        if volume < self.spec.volume_min:
            raise ValueError(
                f"Calculated volume {volume} is below broker minimum {self.spec.volume_min}; "
                "trade rejected rather than increasing risk"
            )
        if volume > self.spec.volume_max:
            volume = self.spec.volume_max
        estimated_loss = loss_per_lot * volume
        return RiskQuote(volume, estimated_loss, risk_money, risk_percent)

    def validate_setup(self, setup: TradeSetup) -> None:
        c = self.constraints()
        if setup.risk_distance < c.min_stop_distance:
            raise ValueError("Stop distance violates broker stop/freeze distance")
        if setup.direction is Direction.BULLISH and not (
            setup.stop_loss < setup.entry < setup.take_profit
        ):
            raise ValueError("Bullish setup geometry is invalid")
        if setup.direction is Direction.BEARISH and not (
            setup.take_profit < setup.entry < setup.stop_loss
        ):
            raise ValueError("Bearish setup geometry is invalid")

    def _volume_digits(self) -> int:
        step = f"{self.spec.volume_step:.10f}".rstrip("0")
        return max(0, len(step.split(".")[1])) if "." in step else 0


def order_side(direction: Direction) -> str:
    return "BUY_LIMIT" if direction is Direction.BULLISH else "SELL_LIMIT"


def pending_price_is_valid(
    direction: Direction,
    entry: float,
    bid: float,
    ask: float,
) -> bool:
    if direction is Direction.BULLISH:
        return entry < ask
    return entry > bid
