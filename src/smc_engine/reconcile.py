from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lifecycle import SetupLifecycle, SetupRegistry, SetupState


@dataclass(frozen=True)
class BrokerLifecycleRecord:
    ticket: int
    setup_id: str
    state: SetupState
    symbol: str
    magic: int
    comment: str


class MT5LifecycleReconciler:
    """Reconcile in-memory setup lifecycle with broker truth after restart."""

    def __init__(self, mt5_module: Any, magic: int, registry: SetupRegistry):
        self.mt5 = mt5_module
        self.magic = magic
        self.registry = registry

    def reconcile(self, symbol: str) -> list[BrokerLifecycleRecord]:
        records: list[BrokerLifecycleRecord] = []

        orders = self.mt5.orders_get(symbol=symbol)
        if orders is None:
            raise RuntimeError(f"Unable to read orders: {self.mt5.last_error()}")

        positions = self.mt5.positions_get(symbol=symbol)
        if positions is None:
            raise RuntimeError(f"Unable to read positions: {self.mt5.last_error()}")

        for order in orders:
            if getattr(order, "magic", None) != self.magic:
                continue
            setup_id = self._setup_id(getattr(order, "comment", ""))
            if setup_id:
                records.append(
                    BrokerLifecycleRecord(
                        ticket=int(order.ticket),
                        setup_id=setup_id,
                        state=SetupState.ORDER_PLACED,
                        symbol=symbol,
                        magic=self.magic,
                        comment=str(order.comment),
                    )
                )

        for position in positions:
            if getattr(position, "magic", None) != self.magic:
                continue
            setup_id = self._setup_id(getattr(position, "comment", ""))
            if setup_id:
                records.append(
                    BrokerLifecycleRecord(
                        ticket=int(position.ticket),
                        setup_id=setup_id,
                        state=SetupState.POSITION_MANAGED,
                        symbol=symbol,
                        magic=self.magic,
                        comment=str(position.comment),
                    )
                )

        return records

    @staticmethod
    def _setup_id(comment: str) -> str | None:
        marker = "SETUP-"
        text = str(comment)
        start = text.find(marker)
        return text[start:].split()[0] if start >= 0 else None

    def active_setup_ids(self, symbol: str) -> set[str]:
        return {r.setup_id for r in self.reconcile(symbol)}
