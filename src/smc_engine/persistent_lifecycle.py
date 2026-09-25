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
        self.store.persist_transition(
            self.lifecycle.setup,
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


class ReconciliationKind:
    BROKER_ACTIVE_MATCH = "BROKER_ACTIVE_MATCH"
    BROKER_ACTIVE_UNKNOWN = "BROKER_ACTIVE_UNKNOWN"
    PERSISTED_MISSING_BROKER = "PERSISTED_MISSING_BROKER"
    STATE_MISMATCH = "STATE_MISMATCH"


@dataclass(frozen=True)
class ReconciliationResult:
    kind: str
    setup_id: str
    broker_state: Optional[SetupState] = None
    persisted_state: Optional[SetupState] = None
    ticket: Optional[int] = None


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

    def restore_persisted_active(self, symbol: str) -> list[SetupLifecycle]:
        restored: list[SetupLifecycle] = []
        for row in self.store.active(symbol):
            setup = self.store.load_setup(row["setup_id"])
            if setup is None:
                continue
            lifecycle = SetupLifecycle(setup, row["state"], row["reason"])
            if self.registry.get(setup.id) is None:
                self.registry.add(lifecycle)
            restored.append(lifecycle)
        return restored

    def startup_reconcile(self, symbol: str) -> list[ReconciliationResult]:
        self.restore_persisted_active(symbol)
        broker_records = self.reconciler.reconcile(symbol)
        broker_ids = {record.setup_id for record in broker_records}
        persisted = {row["setup_id"]: row for row in self.store.active(symbol)}

        results: list[ReconciliationResult] = []
        for record in broker_records:
            row = persisted.get(record.setup_id)
            if row is None:
                results.append(ReconciliationResult(
                    ReconciliationKind.BROKER_ACTIVE_UNKNOWN,
                    record.setup_id, record.state, None, record.ticket,
                ))
            elif row["state"] is record.state:
                results.append(ReconciliationResult(
                    ReconciliationKind.BROKER_ACTIVE_MATCH,
                    record.setup_id, record.state, row["state"], record.ticket,
                ))
            else:
                results.append(ReconciliationResult(
                    ReconciliationKind.STATE_MISMATCH,
                    record.setup_id, record.state, row["state"], record.ticket,
                ))
        for setup_id, row in persisted.items():
            if setup_id not in broker_ids:
                results.append(ReconciliationResult(
                    ReconciliationKind.PERSISTED_MISSING_BROKER,
                    setup_id, None, row["state"], row["ticket"],
                ))
        return results

    def can_accept_new_setup(self, symbol: str) -> bool:
        broker_ids = self.reconciler.active_setup_ids(symbol)
        persisted = self.store.active(symbol)
        persisted_ids = {row["setup_id"] for row in persisted}
        return not broker_ids and not persisted_ids
