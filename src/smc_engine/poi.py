
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .models import Direction, POI, POIState


@dataclass(frozen=True)
class DisplacementConfig:
    atr_period: int = 14
    body_atr_multiple: float = 1.5
    min_close_location: float = 0.60


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def detect_displacement(
    df: pd.DataFrame,
    config: DisplacementConfig = DisplacementConfig(),
) -> pd.DataFrame:
    """Annotate completed candles with deterministic displacement features."""
    if len(df) == 0:
        return df.copy()

    out = df.copy()
    out["atr"] = calculate_atr(out, config.atr_period)
    out["body"] = (out["close"] - out["open"]).abs()
    out["range"] = out["high"] - out["low"]
    out["body_atr_ratio"] = out["body"] / out["atr"].replace(0, np.nan)
    out["close_location"] = np.where(
        out["range"] > 0,
        np.where(
            out["close"] >= out["open"],
            (out["close"] - out["low"]) / out["range"],
            (out["high"] - out["close"]) / out["range"],
        ),
        0.0,
    )
    out["displacement_bullish"] = (
        (out["close"] > out["open"])
        & (out["body_atr_ratio"] >= config.body_atr_multiple)
        & (out["close_location"] >= config.min_close_location)
    )
    out["displacement_bearish"] = (
        (out["close"] < out["open"])
        & (out["body_atr_ratio"] >= config.body_atr_multiple)
        & (out["close_location"] >= config.min_close_location)
    )
    return out


def build_d1_pois(
    df: pd.DataFrame,
    lookback_bars: int = 90,
    config: DisplacementConfig = DisplacementConfig(),
) -> list[POI]:
    """Create D1 demand/supply POIs from displacement candles.

    The candle itself is used as the initial zone definition. Mitigation and
    invalidation are handled separately so the POI lifecycle is explicit.
    """
    if lookback_bars < config.atr_period + 1:
        raise ValueError("lookback_bars must leave enough history for ATR")

    work = detect_displacement(df, config).tail(lookback_bars).reset_index(drop=True)
    pois: list[POI] = []
    for i, row in work.iterrows():
        if bool(row["displacement_bullish"]):
            direction = Direction.BULLISH
        elif bool(row["displacement_bearish"]):
            direction = Direction.BEARISH
        else:
            continue

        pois.append(
            POI(
                id=f"D1-POI-{i}",
                direction=direction,
                timeframe="D1",
                low=float(row["low"]),
                high=float(row["high"]),
                created_index=i,
                created_time=row["time"],
            )
        )
    return pois


def update_poi_lifecycle(
    poi: POI,
    df: pd.DataFrame,
    current_index: Optional[int] = None,
) -> POI:
    """Update one POI through ACTIVE -> TOUCHED/MITIGATED/INVALIDATED.

    For a bullish demand POI, a close below the zone invalidates it.
    For a bearish supply POI, a close above the zone invalidates it.

    A wick into a zone is a touch. A closed candle that trades through the
    zone's opposing boundary is considered mitigated. The exact mitigation
    policy can be tightened later without changing the POI contract.
    """
    out = POI(
        id=poi.id,
        direction=poi.direction,
        timeframe=poi.timeframe,
        low=poi.low,
        high=poi.high,
        created_index=poi.created_index,
        created_time=poi.created_time,
        state=poi.state,
        mitigated_index=poi.mitigated_index,
        invalidated_index=poi.invalidated_index,
        touches=poi.touches,
    )

    start = max(poi.created_index + 1, 0)
    end = len(df) if current_index is None else min(current_index + 1, len(df))
    for i in range(start, end):
        row = df.iloc[i]
        if out.direction is Direction.BULLISH:
            if row["close"] < out.low:
                out.state = POIState.INVALIDATED
                out.invalidated_index = i
                return out
            if row["low"] <= out.high and row["high"] >= out.low:
                out.touches += 1
                out.state = POIState.TOUCHED
            if row["close"] <= out.high and row["low"] <= out.low:
                out.state = POIState.MITIGATED
                out.mitigated_index = i
                return out
        else:
            if row["close"] > out.high:
                out.state = POIState.INVALIDATED
                out.invalidated_index = i
                return out
            if row["high"] >= out.low and row["low"] <= out.high:
                out.touches += 1
                out.state = POIState.TOUCHED
            if row["close"] >= out.low and row["high"] >= out.high:
                out.state = POIState.MITIGATED
                out.mitigated_index = i
                return out
    return out


def active_unmitigated_pois(
    df: pd.DataFrame,
    lookback_bars: int = 90,
    config: DisplacementConfig = DisplacementConfig(),
) -> list[POI]:
    """Return only D1 POIs that remain valid and unmitigated at the latest close."""
    pois = build_d1_pois(df, lookback_bars, config)
    active: list[POI] = []
    for poi in pois:
        state = update_poi_lifecycle(poi, df)
        if state.state in {POIState.ACTIVE, POIState.TOUCHED}:
            active.append(state)
    return active


def price_is_at_poi(
    close: float,
    poi: POI,
    tolerance: float = 0.0,
) -> bool:
    return poi.contains(close, tolerance)
