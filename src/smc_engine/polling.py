from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .dry_run import DryRunEngine, DryRunReport
from .market import MT5MarketData
from .persistent_lifecycle import PersistentLifecycleCoordinator


@dataclass
class PollState:
    last_closed_m15_time: Optional[pd.Timestamp] = None


class LivePollingCoordinator:
    """Poll MT5 but analyze only when a new closed M15 candle exists."""

    def __init__(self, market: MT5MarketData, dry_run: DryRunEngine, lifecycle: PersistentLifecycleCoordinator | None = None):
        self.market = market
        self.dry_run = dry_run
        self.state = PollState()
        self.lifecycle = lifecycle

    def startup(self) -> None:
        if self.lifecycle is not None:
            self.lifecycle.startup_reconcile(self.market.symbol)

    def poll_once(self, balance: float) -> Optional[DryRunReport]:
        bars = self.market.closed_bars(15, count=1)
        closed_time = pd.Timestamp(bars.iloc[-1]["time"])
        if self.state.last_closed_m15_time == closed_time:
            return None
        self.state.last_closed_m15_time = closed_time
        # Do not analyze a replacement setup while a restored/persisted setup is active.
        if self.lifecycle is not None and not self.lifecycle.can_accept_new_setup(self.market.symbol):
            return DryRunReport(
                self.market.symbol, None, None, None, None, None, None, None,
                "POLL: active persisted or broker setup exists; no replacement analysis",
            )
        return self.dry_run.run_once(balance)
