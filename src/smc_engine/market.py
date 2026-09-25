
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import MetaTrader5 as mt5
import pandas as pd

@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int
    trade_freeze_level: int
    filling_mode: int

class MT5MarketData:
    """Read-only MT5 adapter. Analysis always uses closed candles."""
    def __init__(self, symbol: str):
        self.symbol = symbol

    def initialize(self) -> None:
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"Unable to select {self.symbol}: {mt5.last_error()}")

    def shutdown(self) -> None:
        mt5.shutdown()

    def symbol_spec(self) -> SymbolSpec:
        info = mt5.symbol_info(self.symbol)
        if info is None:
            raise RuntimeError(f"Unable to read symbol info for {self.symbol}: {mt5.last_error()}")
        return SymbolSpec(
            symbol=self.symbol,
            digits=info.digits,
            point=info.point,
            tick_size=info.trade_tick_size,
            tick_value=info.trade_tick_value,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            trade_stops_level=info.trade_stops_level,
            trade_freeze_level=info.trade_freeze_level,
            filling_mode=info.filling_mode,
        )

    def closed_bars(self, timeframe: int, count: int = 200) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(self.symbol, timeframe, 1, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(
                f"No closed bars returned for {self.symbol}/{timeframe}: {mt5.last_error()}"
            )
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df[["time", "open", "high", "low", "close", "tick_volume"]].reset_index(drop=True)

    def tick(self) -> Any:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise RuntimeError(f"Unable to read tick for {self.symbol}: {mt5.last_error()}")
        return tick
