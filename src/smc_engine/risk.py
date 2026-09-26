from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

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
    """Broker-aware sizing and validation."""
    def __init__(self, spec: SymbolSpec, mt5_module: Any = None):
        self.spec = spec
        self.mt5 = mt5_module

    def constraints(self) -> ExecutionConstraints:
        stop_level = max(self.spec.trade_stops_level, self.spec.trade_freeze_level)
        return ExecutionConstraints(stop_level * self.spec.point, self.spec.volume_min, self.spec.volume_max, self.spec.volume_step, self.spec.tick_size)

    def volume_for_risk(self, balance: float, risk_percent: float, entry: float, stop_loss: float) -> RiskQuote:
        if balance <= 0 or risk_percent <= 0:
            raise ValueError("balance and risk_percent must be positive")
        if entry == stop_loss:
            raise ValueError("entry and stop_loss cannot be equal")
        risk_money = Decimal(str(balance)) * Decimal(str(risk_percent)) / Decimal("100")
        loss_per_lot = Decimal(str(self._loss_per_lot(entry, stop_loss)))
        if loss_per_lot <= 0:
            raise ValueError("calculated loss per lot must be positive")
        step = Decimal(str(self.spec.volume_step))
        volume = float((risk_money / loss_per_lot / step).to_integral_value(rounding=ROUND_DOWN) * step)
        if volume < self.spec.volume_min:
            raise ValueError(f"Calculated volume {volume} is below broker minimum {self.spec.volume_min}; trade rejected rather than increasing risk")
        volume = min(volume, self.spec.volume_max)
        estimated_loss = self._loss_for_volume(volume, entry, stop_loss)
        if estimated_loss > float(risk_money) * 1.000001:
            raise ValueError("broker-calculated loss exceeds requested risk")
        return RiskQuote(volume, estimated_loss, float(risk_money), risk_percent)

    def _loss_per_lot(self, entry: float, stop_loss: float) -> float:
        if self.mt5 is not None:
            order_type = self.mt5.ORDER_TYPE_BUY if entry > stop_loss else self.mt5.ORDER_TYPE_SELL
            value = self.mt5.order_calc_profit(order_type, self.spec.symbol, 1.0, entry, stop_loss)
            if value is not None:
                return abs(float(value))
        tick_size = Decimal(str(self.spec.tick_size))
        tick_value = Decimal(str(self.spec.tick_value))
        if tick_size <= 0 or tick_value <= 0:
            raise ValueError("broker tick size/value must be positive")
        distance = abs(Decimal(str(entry)) - Decimal(str(stop_loss)))
        return float(distance / tick_size * tick_value)

    def _loss_for_volume(self, volume: float, entry: float, stop_loss: float) -> float:
        if self.mt5 is not None:
            order_type = self.mt5.ORDER_TYPE_BUY if entry > stop_loss else self.mt5.ORDER_TYPE_SELL
            value = self.mt5.order_calc_profit(order_type, self.spec.symbol, volume, entry, stop_loss)
            if value is not None:
                return abs(float(value))
        return self._loss_per_lot(entry, stop_loss) * volume

    def validate_setup(self, setup: TradeSetup) -> None:
        c = self.constraints()
        if setup.risk_distance < c.min_stop_distance:
            raise ValueError("Stop distance violates broker stop/freeze distance")
        if setup.direction is Direction.BULLISH and not (setup.stop_loss < setup.entry < setup.take_profit):
            raise ValueError("Bullish setup geometry is invalid")
        if setup.direction is Direction.BEARISH and not (setup.take_profit < setup.entry < setup.stop_loss):
            raise ValueError("Bearish setup geometry is invalid")

def order_side(direction: Direction) -> str:
    return "BUY_LIMIT" if direction is Direction.BULLISH else "SELL_LIMIT"

def pending_price_is_valid(direction: Direction, entry: float, bid: float, ask: float) -> bool:
    return entry < ask if direction is Direction.BULLISH else entry > bid

def allocate_risk_budget(setups: list[TradeSetup], max_total_risk_percent: float) -> list[TradeSetup]:
    """Select setups without exceeding aggregate declared risk."""
    if max_total_risk_percent <= 0:
        raise ValueError("max_total_risk_percent must be positive")
    ordered = sorted(setups, key=lambda s: (s.created_time, -s.risk_reward, s.id))
    selected: list[TradeSetup] = []
    used = 0.0
    for setup in ordered:
        if setup.risk_percent <= 0:
            raise ValueError("setup risk_percent must be positive")
        if used + setup.risk_percent <= max_total_risk_percent + 1e-12:
            selected.append(setup)
            used += setup.risk_percent
    return selected

def allocate_portfolio_risk_budget(
    setups: list[TradeSetup],
    existing_risk_percent: float,
    max_total_risk_percent: float,
) -> list[TradeSetup]:
    """Select new setups after reserving risk for existing exposure."""
    if existing_risk_percent < 0:
        raise ValueError("existing_risk_percent cannot be negative")
    if max_total_risk_percent <= 0:
        raise ValueError("max_total_risk_percent must be positive")
    if existing_risk_percent > max_total_risk_percent:
        return []
    remaining = Decimal(str(max_total_risk_percent)) - Decimal(str(existing_risk_percent))
    ordered = sorted(setups, key=lambda s: (s.created_time, -s.risk_reward, s.id))
    selected: list[TradeSetup] = []
    used = Decimal("0")
    for setup in ordered:
        if setup.risk_percent <= 0:
            raise ValueError("setup risk_percent must be positive")
        risk = Decimal(str(setup.risk_percent))
        if used + risk <= remaining:
            selected.append(setup)
            used += risk
    return selected
