from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Direction
from .risk import RiskEngine, pending_price_is_valid
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
    filling_mode: int | None = None


class MT5ExecutionAdapter:
    """Broker execution boundary. SMC logic must not live here."""

    def __init__(self, mt5_module: Any, risk: RiskEngine, magic: int = 202609):
        self.mt5 = mt5_module
        self.risk = risk
        self.magic = magic

    def _normalize_price(self, price: float) -> float:
        tick_size = self.risk.spec.tick_size or self.risk.spec.point
        if tick_size <= 0:
            raise ValueError("symbol tick size must be positive")
        normalized = round(round(price / tick_size) * tick_size, self.risk.spec.digits)
        return normalized

    def build_limit_request(
        self,
        setup: TradeSetup,
        balance: float,
        bid: float,
        ask: float,
        comment: str | None = None,
    ) -> PendingOrderRequest:
        self.risk.validate_setup(setup)
        if not pending_price_is_valid(setup.direction, setup.entry, bid, ask):
            raise ValueError("Limit entry is no longer valid at current market")

        quote = self.risk.volume_for_risk(balance, setup.risk_percent, setup.entry, setup.stop_loss)
        filling = self._resolve_filling_mode()
        return PendingOrderRequest(
            symbol=setup.symbol,
            direction=setup.direction,
            volume=quote.volume,
            price=self._normalize_price(setup.entry),
            stop_loss=self._normalize_price(setup.stop_loss),
            take_profit=self._normalize_price(setup.take_profit),
            magic=self.magic,
            comment=comment or self._broker_comment(setup.id),
            filling_mode=filling,
        )

    @staticmethod
    def _broker_comment(setup_id: str) -> str:
        comment = f"SMC {setup_id if setup_id.startswith("SETUP-") else "SETUP-" + setup_id}"
        if len(comment) > 31:
            raise ValueError("setup id is too long for the MT5 order comment identity")
        return comment

    def _resolve_filling_mode(self) -> int | None:
        if not hasattr(self.mt5, "symbol_info"):
            return self.risk.spec.filling_mode or None
        info = self.mt5.symbol_info(self.risk.spec.symbol)
        if info is None:
            raise RuntimeError("Unable to read symbol information")
        supported = getattr(info, "filling_mode", 0)
        for mode in (
            getattr(self.mt5, "ORDER_FILLING_RETURN", None),
            getattr(self.mt5, "ORDER_FILLING_IOC", None),
            getattr(self.mt5, "ORDER_FILLING_FOK", None),
        ):
            if mode is not None and supported & mode:
                return mode
        return None

    def to_mt5_request(self, request: PendingOrderRequest) -> dict[str, Any]:
        order_type = (
            self.mt5.ORDER_TYPE_BUY_LIMIT
            if request.direction is Direction.BULLISH
            else self.mt5.ORDER_TYPE_SELL_LIMIT
        )
        payload = {
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
        if request.filling_mode is not None:
            payload["type_filling"] = request.filling_mode
        return payload

    def preflight(self, mt5_request: dict[str, Any]) -> Any:
        result = self.mt5.order_check(mt5_request)
        if result is None:
            raise RuntimeError(f"MT5 order_check failed: {self.mt5.last_error()}")
        retcode = getattr(result, "retcode", None)
        if retcode is None and isinstance(result, dict):
            retcode = result.get("retcode")
        if retcode not in (None, 0):
            raise ValueError(f"MT5 order_check rejected request retcode={retcode}")
        return result

    def send(self, mt5_request: dict[str, Any]) -> Any:
        return self.mt5.order_send(mt5_request)
