from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .lifecycle import SetupLifecycle, SetupRegistry, SetupState
from .reconcile import MT5LifecycleReconciler
from .store import SetupStore


@dataclass
class PersistentLifecycle:
    lifecycle: SetupLifecycle
    store: SetupStore

    def transition(self, new_state: SetupState, event_time: object, reason: str, ticket: Optional[int] = None) -> None:
        previous = self.lifecycle.state
        previous_reason = self.lifecycle.reason
        self.lifecycle.transition(new_state, reason)
        try:
            self.store.persist_transition(self.lifecycle.setup, previous, new_state, event_time, reason, ticket=ticket)
        except Exception:
            self.lifecycle.state = previous
            self.lifecycle.reason = previous_reason
            raise

    def persist(self, event_time: object, ticket: Optional[int] = None) -> None:
        self.store.upsert_setup(
            self.lifecycle.setup,
            self.lifecycle.state,
            event_time,
            ticket=ticket,
            reason=self.lifecycle.reason,
        )


@dataclass(frozen=True)
class SubmissionResult:
    state: SetupState
    broker_result: Any = None
    ticket: Optional[int] = None
    ambiguous: bool = False

class ReconciliationKind:
    BROKER_ACTIVE_MATCH = "BROKER_ACTIVE_MATCH"
    BROKER_ACTIVE_UNKNOWN = "BROKER_ACTIVE_UNKNOWN"
    PERSISTED_MISSING_BROKER = "PERSISTED_MISSING_BROKER"
    STATE_MISMATCH = "STATE_MISMATCH"
    SUBMISSION_CONFIRMED = "SUBMISSION_CONFIRMED"
    BROKER_IDENTITY_CONFLICT = "BROKER_IDENTITY_CONFLICT"


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
        records_by_setup: dict[str, list[Any]] = {}
        for record in broker_records:
            records_by_setup.setdefault(record.setup_id, []).append(record)

        for setup_id, records in records_by_setup.items():
            if len(records) > 1:
                results.append(ReconciliationResult(
                    ReconciliationKind.BROKER_IDENTITY_CONFLICT,
                    setup_id,
                    None,
                    persisted.get(setup_id, {}).get("state"),
                    None,
                ))

        for record in broker_records:
            if len(records_by_setup[record.setup_id]) > 1:
                continue
            row = persisted.get(record.setup_id)
            if row is None:
                results.append(ReconciliationResult(
                    ReconciliationKind.BROKER_ACTIVE_UNKNOWN,
                    record.setup_id, record.state, None, record.ticket,
                ))
            elif row["state"] is SetupState.ORDER_SUBMITTING and record.state is SetupState.ORDER_PLACED:
                lifecycle = self.registry.get(record.setup_id)
                if lifecycle is not None:
                    PersistentLifecycle(lifecycle, self.store).transition(
                        SetupState.ORDER_PLACED,
                        row["updated_time"],
                        "startup reconciliation confirmed broker submission",
                        ticket=record.ticket,
                    )
                results.append(ReconciliationResult(ReconciliationKind.SUBMISSION_CONFIRMED, record.setup_id, record.state, row["state"], record.ticket))
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

    def submit_pending(self, lifecycle: SetupLifecycle, execution: Any, balance: float, bid: float, ask: float, event_time: object) -> SubmissionResult:
        if lifecycle.state is SetupState.EXECUTION_READY:
            PersistentLifecycle(lifecycle, self.store).transition(SetupState.ORDER_PREPARED, event_time, "pending order prepared")
        if lifecycle.state is SetupState.ORDER_PREPARED:
            request = execution.build_limit_request(lifecycle.setup, balance, bid, ask)
            payload = execution.to_mt5_request(request)
            execution.preflight(payload)
            PersistentLifecycle(lifecycle, self.store).transition(SetupState.ORDER_PREFLIGHTED, event_time, "broker preflight passed")
        elif lifecycle.state is SetupState.ORDER_PREFLIGHTED:
            # A restart may have occurred after the previous preflight. Rebuild
            # and re-preflight the exact request that will be submitted.
            request = execution.build_limit_request(lifecycle.setup, balance, bid, ask)
            payload = execution.to_mt5_request(request)
            execution.preflight(payload)
        else:
            raise ValueError(f"Cannot submit lifecycle in state {lifecycle.state.value}")
        PersistentLifecycle(lifecycle, self.store).transition(SetupState.ORDER_SUBMITTING, event_time, "durable submission intent recorded")
        try:
            broker_result = execution.send(payload)
        except Exception as exc:
            return SubmissionResult(SetupState.ORDER_SUBMITTING, broker_result=exc, ambiguous=True)
        if broker_result is None:
            return SubmissionResult(SetupState.ORDER_SUBMITTING, broker_result=execution.mt5.last_error(), ambiguous=True)
        retcode = getattr(broker_result, "retcode", None)
        if retcode is None and isinstance(broker_result, dict):
            retcode = broker_result.get("retcode")
        success_codes = set()
        for name in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED"):
            value = getattr(execution.mt5, name, None)
            if value is not None:
                success_codes.add(value)
        if retcode not in success_codes:
            ambiguous_codes = {x for x in (getattr(execution.mt5, "TRADE_RETCODE_REQUOTE", None), getattr(execution.mt5, "TRADE_RETCODE_TIMEOUT", None), getattr(execution.mt5, "TRADE_RETCODE_CONNECTION", None)) if x is not None}
            if retcode in ambiguous_codes:
                return SubmissionResult(SetupState.ORDER_SUBMITTING, broker_result=broker_result, ambiguous=True)
            PersistentLifecycle(lifecycle, self.store).transition(SetupState.BROKER_REJECTED, event_time, f"broker rejected pending order retcode={retcode}")
            return SubmissionResult(SetupState.BROKER_REJECTED, broker_result=broker_result)
        ticket = None
        if isinstance(broker_result, dict):
            ticket = broker_result.get("order") or broker_result.get("ticket")
        else:
            ticket = getattr(broker_result, "order", None) or getattr(broker_result, "ticket", None)
        if ticket is None:
            return SubmissionResult(
                SetupState.ORDER_SUBMITTING,
                broker_result=broker_result,
                ambiguous=True,
            )
        PersistentLifecycle(lifecycle, self.store).transition(
            SetupState.ORDER_PLACED,
            event_time,
            "broker accepted pending order",
            ticket=int(ticket),
        )
        return SubmissionResult(SetupState.ORDER_PLACED, broker_result=broker_result, ticket=int(ticket))

    def can_accept_new_setup(self, symbol: str) -> bool:
        broker_ids = self.reconciler.active_setup_ids(symbol)
        persisted = self.store.active(symbol)
        persisted_ids = {row["setup_id"] for row in persisted}
        return not broker_ids and not persisted_ids
