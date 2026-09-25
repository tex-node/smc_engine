
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Direction
from .risk import RiskEngine, order_side, pending_price_is_valid
from .setup import TradeSetup


@dataclass(frozen=True)
class PendingOrderRequest:
    symbol: str
    direction: Direction
    volume: float
    price: float
    stop_loss: float
    take_profit: float
    magic: int
    comment: str


class MT5ExecutionAdapter:
    """Thin execution boundary. SMC logic must not live here."""

    def __init__(self, mt5_module: Any, risk: RiskEngine, magic: int = 202609):
        self.mt5 = mt5_module
        self.risk = risk
        self.magic = magic

    def build_limit_request(
        self,
        setup: TradeSetup,
        balance: float,
        bid: float,
        ask: float,
        comment: str = "SMC",
    ) -> PendingOrderRequest:
        self.risk.validate_setup(setup)
        if not pending_price_is_valid(setup.direction, setup.entry, bid, ask):
            raise ValueError("Limit entry is no longer valid at current market")

        quote = self.risk.volume_for_risk(
            balance,
            setup.risk_percent,
            setup.entry,
            setup.stop_loss,
        )
        return PendingOrderRequest(
            symbol=setup.symbol,
            direction=setup.direction,
            volume=quote.volume,
            price=setup.entry,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            magic=self.magic,
            comment=comment,
        )

    def to_mt5_request(self, request: PendingOrderRequest) -> dict[str, Any]:
        order_type = (
            self.mt5.ORDER_TYPE_BUY_LIMIT
            if request.direction is Direction.BULLISH
            else self.mt5.ORDER_TYPE_SELL_LIMIT
        )
        return {
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": request.symbol,
            "volume": request.volume,
            "type": order_type,
            "price": request.price,
            "sl": request.stop_loss,
            "tp": request.take_profit,
            "deviation": 0,
            "magic": request.magic,
            "comment": request.comment,
            "type_time": self.mt5.ORDER_TIME_GTC,
        }

    def preflight(self, mt5_request: dict[str, Any]) -> Any:
        """Broker-side validation without sending an order."""
        result = self.mt5.order_check(mt5_request)
        if result is None:
            raise RuntimeError(f"MT5 order_check failed: {self.mt5.last_error()}")
        return result

    def send(self, mt5_request: dict[str, Any]) -> Any:
        return self.mt5.order_send(mt5_request)
