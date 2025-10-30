from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd


@dataclass(slots=True)
class ChandelierConfig:
    atr_period: int = 22
    atr_multiplier: float = 3.0
    drawdown_pct: float | None = None


class NotEnoughDataError(ValueError):
    pass


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["adj_close"].shift(1)
    high = df["adj_high"]
    low = df["adj_low"]
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.name = "true_range"
    return tr


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    if len(df) < period:
        raise NotEnoughDataError(f"Need at least {period} rows for ATR")
    tr = _true_range(df)
    atr_series = tr.rolling(window=period, min_periods=period).mean()
    return atr_series


def chandelier_exit(df: pd.DataFrame, config: ChandelierConfig) -> pd.Series:
    atr_series = atr(df, config.atr_period)
    highest_high = df["adj_high"].rolling(window=config.atr_period, min_periods=config.atr_period).max()
    chandelier = highest_high - config.atr_multiplier * atr_series
    if config.drawdown_pct is not None:
        peak_price = df["adj_close"].cummax()
        drawdown_stop = peak_price * (1 - config.drawdown_pct)
        chandelier = chandelier.combine(drawdown_stop, max)
    chandelier.name = "chandelier"
    return chandelier


def trailing_stop(
    df: pd.DataFrame, config: ChandelierConfig, previous_stop: float | None = None
) -> pd.Series:
    chandelier_series = chandelier_exit(df, config)
    stop_prices: list[float] = []
    last_stop = previous_stop
    for chandelier_value in chandelier_series:
        if pd.isna(chandelier_value):
            stop_prices.append(float("nan"))
            continue
        if last_stop is None or pd.isna(last_stop):
            last_stop = chandelier_value
        else:
            last_stop = max(last_stop, chandelier_value)
        stop_prices.append(last_stop)
    return pd.Series(stop_prices, index=df.index, name="stop")
