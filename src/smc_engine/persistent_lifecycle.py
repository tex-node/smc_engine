from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .lifecycle import SetupLifecycle, SetupRegistry, SetupState
from .reconcile import MT5LifecycleReconciler
from .store import SetupStore


@dataclass
class PersistentLifecycle:
    lifecycle: SetupLifecycle
    store: SetupStore

    def transition(self, new_state: SetupState, event_time: object, reason: str) -> None:
        previous = self.lifecycle.state
        self.lifecycle.transition(new_state, reason)
        self.store.upsert_setup(
            self.lifecycle.setup,
            self.lifecycle.state,
            event_time,
            reason=reason,
        )
        self.store.record_transition(
            self.lifecycle.setup.id,
            previous,
            new_state,
            event_time,
            reason,
        )

    def persist(self, event_time: object, ticket: Optional[int] = None) -> None:
        self.store.upsert_setup(
            self.lifecycle.setup,
            self.lifecycle.state,
            event_time,
            ticket=ticket,
            reason=self.lifecycle.reason,
        )


class PersistentLifecycleCoordinator:
    """Startup/restart boundary joining SQLite strategy state and MT5 truth."""

    def __init__(
        self,
        store: SetupStore,
        reconciler: MT5LifecycleReconciler,
        registry: SetupRegistry,
    ):
        self.store = store
        self.reconciler = reconciler
        self.registry = registry

    def startup_reconcile(self, symbol: str) -> set[str]:
        broker_records = self.reconciler.reconcile(symbol)
        broker_ids = {record.setup_id for record in broker_records}
        persisted = {row["setup_id"]: row for row in self.store.active(symbol)}

        # Broker execution state wins for identities that were actually submitted.
        # Persisted strategy state is retained for setups that have not reached MT5.
        for record in broker_records:
            row = persisted.get(record.setup_id)
            if row is not None and row["state"] is not record.state:
                # Do not manufacture a TradeSetup here; update only after the
                # original setup has been loaded by the caller.
                continue

        return broker_ids

    def can_accept_new_setup(self, symbol: str) -> bool:
        broker_ids = self.reconciler.active_setup_ids(symbol)
        persisted = self.store.active(symbol)
        persisted_ids = {row["setup_id"] for row in persisted}
        return not broker_ids and not persisted_ids
