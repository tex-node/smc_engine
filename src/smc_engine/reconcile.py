from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re

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
            setup_id = self.setup_id_from_comment(getattr(order, "comment", "")) or self._unknown_setup_id("ORDER", int(order.ticket))
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
            setup_id = self.setup_id_from_comment(getattr(position, "comment", "")) or self._unknown_setup_id("POSITION", int(position.ticket))
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
    def _unknown_setup_id(kind: str, ticket: int) -> str:
        return f"BROKER-UNKNOWN-{kind}-{ticket}"

    @staticmethod
    def setup_id_from_comment(comment: str) -> str | None:
        text = str(comment)
        match = re.search(r"(?<!\w)(SETUP-[^\s]+)", text)
        return match.group(1) if match else None

    @classmethod
    def _setup_id(cls, comment: str) -> str | None:
        return cls.setup_id_from_comment(comment)

    def active_setup_ids(self, symbol: str) -> set[str]:
        return {r.setup_id for r in self.reconcile(symbol)}

    def find_setup(self, symbol: str, setup_id: str) -> list[BrokerLifecycleRecord]:
        return [r for r in self.reconcile(symbol) if r.setup_id == setup_id]

    def history_order(self, ticket: int) -> Any:
        """Return the broker's historical order for a known ticket without changing lifecycle state."""
        history_get = getattr(self.mt5, "history_orders_get", None)
        if history_get is None:
            return None
        records = history_get(ticket=int(ticket))
        if records is None:
            raise RuntimeError(f"Unable to read order history for ticket {ticket}: {self.mt5.last_error()}")
        return records[0] if records else None
