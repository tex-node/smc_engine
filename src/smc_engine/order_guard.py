from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .models import Direction
from .setup import TradeSetup


@dataclass(frozen=True)
class BrokerOrder:
    ticket: int
    symbol: str
    magic: int
    comment: str
    direction: Direction
    price: float
    stop_loss: float
    take_profit: float
    volume: float


class MT5OrderGuard:
    """Idempotency and live invalidation boundary around MT5 orders."""

    def __init__(self, mt5_module: Any, magic: int):
        self.mt5 = mt5_module
        self.magic = magic

    def pending_orders(self, symbol: str) -> list[Any]:
        orders = self.mt5.orders_get(symbol=symbol)
        if orders is None:
            error = self.mt5.last_error()
            raise RuntimeError(f"Unable to read pending orders: {error}")
        return [
            o for o in orders
            if getattr(o, "magic", None) == self.magic
        ]

    def positions(self, symbol: str) -> list[Any]:
        positions = self.mt5.positions_get(symbol=symbol)
        if positions is None:
            error = self.mt5.last_error()
            raise RuntimeError(f"Unable to read positions: {error}")
        return [
            p for p in positions
            if getattr(p, "magic", None) == self.magic
        ]

    def has_active_identity(self, setup: TradeSetup) -> bool:
        identity = setup.id
        for order in self.pending_orders(setup.symbol):
            if identity in str(getattr(order, "comment", "")):
                return True
        for position in self.positions(setup.symbol):
            if identity in str(getattr(position, "comment", "")):
                return True
        return False

    def setup_is_still_valid(
        self,
        setup: TradeSetup,
        bid: float,
        ask: float,
    ) -> bool:
        if setup.direction is Direction.BULLISH:
            return bid > setup.invalidation_level
        return ask < setup.invalidation_level

    def cancel_pending(self, ticket: int) -> Any:
        request = {
            "action": self.mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        result = self.mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"Pending cancellation failed: {self.mt5.last_error()}")
        return result
