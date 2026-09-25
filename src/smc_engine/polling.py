from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .dry_run import DryRunEngine, DryRunReport
from .market import MT5MarketData


@dataclass
class PollState:
    last_closed_m15_time: Optional[pd.Timestamp] = None


class LivePollingCoordinator:
    """Poll MT5 but analyze only when a new closed M15 candle exists."""

    def __init__(self, market: MT5MarketData, dry_run: DryRunEngine):
        self.market = market
        self.dry_run = dry_run
        self.state = PollState()

    def poll_once(self, balance: float) -> Optional[DryRunReport]:
        bars = self.market.closed_bars(15, count=1)
        closed_time = pd.Timestamp(bars.iloc[-1]["time"])
        if self.state.last_closed_m15_time == closed_time:
            return None
        self.state.last_closed_m15_time = closed_time
        return self.dry_run.run_once(balance)
