from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

from .models import Direction
from .risk import RiskEngine
from .setup import TradeSetup


@dataclass(frozen=True)
class MarketQuote:
    bid: float
    ask: float


@dataclass(frozen=True)
class NormalizedOrder:
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    volume: float


@dataclass(frozen=True)
class ExecutionDecision:
    executable: bool
    order: Optional[NormalizedOrder]
    reason: str


class ExecutionPolicy:
    """Single deterministic contract shared by historical and live execution paths."""

    def __init__(self, risk_engine: RiskEngine):
        self.risk_engine = risk_engine

    def normalize_price(self, price: float) -> float:
        tick = Decimal(str(self.risk_engine.spec.tick_size))
        if tick <= 0:
            raise ValueError("broker tick size must be positive")
        value = Decimal(str(price)) / tick
        return float(value.to_integral_value(rounding=ROUND_HALF_UP) * tick)

    def normalize_volume(self, volume: float) -> float:
        step = Decimal(str(self.risk_engine.spec.volume_step))
        if step <= 0:
            raise ValueError("broker volume step must be positive")
        value = Decimal(str(volume)) / step
        return float(value.to_integral_value(rounding=ROUND_DOWN) * step)

    def build_order(self, setup: TradeSetup, balance: float) -> NormalizedOrder:
        entry = self.normalize_price(setup.entry)
        stop = self.normalize_price(setup.stop_loss)
        target = self.normalize_price(setup.take_profit)
        self._validate_geometry(setup.direction, entry, stop, target)
        normalized_setup = TradeSetup(
            id=setup.id, symbol=setup.symbol, direction=setup.direction,
            created_time=setup.created_time, poi_id=setup.poi_id, sweep_id=setup.sweep_id,
            csd_id=setup.csd_id, protected_level=setup.protected_level,
            order_block_id=setup.order_block_id, inducement_id=setup.inducement_id,
            entry=entry, stop_loss=stop, take_profit=target, irl_swing_id=setup.irl_swing_id,
            invalidation_level=setup.invalidation_level, risk_percent=setup.risk_percent,
        )
        self.risk_engine.validate_setup(normalized_setup)
        quote = self.risk_engine.volume_for_risk(balance, setup.risk_percent, entry, stop)
        return NormalizedOrder(
            side="BUY_LIMIT" if setup.direction is Direction.BULLISH else "SELL_LIMIT",
            entry=entry, stop_loss=stop, take_profit=target,
            volume=self.normalize_volume(quote.volume),
        )

    def evaluate_pending(self, setup: TradeSetup, quote: MarketQuote, balance: float) -> ExecutionDecision:
        try:
            order = self.build_order(setup, balance)
        except ValueError as exc:
            return ExecutionDecision(False, None, str(exc))
        valid = order.entry < quote.ask if setup.direction is Direction.BULLISH else order.entry > quote.bid
        if not valid:
            return ExecutionDecision(False, None, "pending_entry_is_not_executable_at_current_quote")
        return ExecutionDecision(True, order, "executable")

    @staticmethod
    def _validate_geometry(direction: Direction, entry: float, stop: float, target: float) -> None:
        if direction is Direction.BULLISH and not (stop < entry < target):
            raise ValueError("Bullish normalized order geometry is invalid")
        if direction is Direction.BEARISH and not (target < entry < stop):
            raise ValueError("Bearish normalized order geometry is invalid")
