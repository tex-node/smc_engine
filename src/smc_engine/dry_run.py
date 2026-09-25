
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .analyzer import MT5Analyzer, LiveAnalysis
from .execution import MT5ExecutionAdapter, PendingOrderRequest
from .lifecycle import SetupState
from .market import MT5MarketData
from .order_guard import MT5OrderGuard
from .risk import RiskEngine


@dataclass(frozen=True)
class DryRunReport:
    symbol: str
    setup_id: Optional[str]
    state: Optional[SetupState]
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    volume: Optional[float]
    estimated_loss: Optional[float]
    message: str


class DryRunEngine:
    """Read-only production orchestration.

    It fetches MT5 data, analyzes a causal setup, validates broker-aware risk,
    and produces the exact pending-order intent without calling order_send().
    """

    def __init__(
        self,
        market: MT5MarketData,
        analyzer: MT5Analyzer,
        risk: RiskEngine,
        execution: Optional[MT5ExecutionAdapter] = None,
    ):
        self.market = market
        self.analyzer = analyzer
        self.risk = risk
        self.execution = execution
        self.order_guard = order_guard

    def run_once(self, balance: float) -> DryRunReport:
        result: LiveAnalysis = self.analyzer.analyze_once()
        candidate = result.candidate
        if candidate is None:
            return DryRunReport(
                result.symbol, None, None, None, None, None, None, None,
                "No causal TradeSetup detected",
            )

        setup = candidate.setup
        try:
            self.risk.validate_setup(setup)
            quote = self.risk.volume_for_risk(
                balance,
                setup.risk_percent,
                setup.entry,
                setup.stop_loss,
            )
            return DryRunReport(
                setup.symbol,
                setup.id,
                SetupState.EXECUTION_READY,
                setup.entry,
                setup.stop_loss,
                setup.take_profit,
                quote.volume,
                quote.estimated_loss,
                "DRY RUN: setup and broker risk validation passed; no order sent",
            )
        except ValueError as exc:
            return DryRunReport(
                setup.symbol,
                setup.id,
                SetupState.RISK_REJECTED,
                setup.entry,
                setup.stop_loss,
                setup.take_profit,
                None,
                None,
                f"DRY RUN: rejected by risk validation: {exc}",
            )
