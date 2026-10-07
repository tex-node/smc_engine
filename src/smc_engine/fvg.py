from __future__ import annotations

"""Fair Value Gap (3-candle imbalance) detection primitive.

Backend-of-truth for FVG geometry and mitigation state. The web layer may
only display what this module reports; it must never compute gaps itself.
"""

from dataclasses import dataclass

import pandas as pd

from .models import Direction


@dataclass
class FairValueGap:
    id: str
    direction: Direction
    top: float
    bottom: float
    created_index: int
    created_time: object
    state: str = "ACTIVE"  # ACTIVE | TOUCHED | MITIGATED
    mitigation_index: int | None = None
    mitigation_time: object = None

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def detect_fvgs(df: pd.DataFrame) -> list[FairValueGap]:
    """Three-candle imbalance: bullish when candle i low exceeds candle i-2 high."""
    fvgs: list[FairValueGap] = []
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    times = df["time"].tolist()
    for i in range(2, len(df)):
        if lows[i] > highs[i - 2]:
            gap = FairValueGap(
                id=f"FVG-B-{int(pd.Timestamp(times[i]).value)}", direction=Direction.BULLISH,
                top=lows[i], bottom=highs[i - 2], created_index=i,
                created_time=times[i],
            )
            fvgs.append(gap)
        elif highs[i] < lows[i - 2]:
            gap = FairValueGap(
                id=f"FVG-S-{int(pd.Timestamp(times[i]).value)}", direction=Direction.BEARISH,
                top=lows[i - 2], bottom=highs[i], created_index=i,
                created_time=times[i],
            )
            fvgs.append(gap)
    _apply_mitigation(df, fvgs)
    return fvgs


def _apply_mitigation(df: pd.DataFrame, fvgs: list[FairValueGap]) -> None:
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    times = df["time"].tolist()
    for k, gap in enumerate(fvgs):
        state = "ACTIVE"
        mitigation_index = None
        mitigation_time = None
        start = gap.created_index + 1
        end = fvgs[k + 1].created_index if k + 1 < len(fvgs) else len(df) - 1
        touched_at = None
        for i in range(start, end + 1):
            entered = (highs[i] >= gap.bottom and lows[i] <= gap.top) or (
                gap.bottom <= closes[i] <= gap.top
            )
            if entered and touched_at is None:
                touched_at = i
            fully_through = (
                lows[i] <= gap.bottom if gap.direction is Direction.BULLISH
                else highs[i] >= gap.top
            )
            if fully_through:
                state = "MITIGATED"
                mitigation_index = i
                mitigation_time = times[i]
                break
        if state != "MITIGATED" and touched_at is not None:
            state = "TOUCHED"
        gap.state = state
        gap.mitigation_index = mitigation_index
        gap.mitigation_time = mitigation_time
