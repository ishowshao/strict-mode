from __future__ import annotations

import abc
from datetime import date
from typing import Protocol

import pandas as pd


class AdjustedDailyBar:
    """Lightweight wrapper around a pandas-style DataFrame."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        self.symbol: str | None = None

    def __getattr__(self, item: str):
        return getattr(self._frame, item)

    def __getitem__(self, key):
        return self._frame[key]

    def __len__(self) -> int:
        return len(self._frame)

    def __iter__(self):
        return iter(self._frame)

    def set_symbol(self, symbol: str) -> None:
        self.symbol = symbol

    def tail(self, count: int) -> "AdjustedDailyBar":
        trimmed = AdjustedDailyBar(self._frame.tail(count))
        trimmed.symbol = self.symbol
        return trimmed

    def to_dataframe(self) -> pd.DataFrame:
        """Return a copy of the underlying pandas DataFrame for downstream use."""
        return self._frame.copy()

    @property
    def frame(self) -> pd.DataFrame:
        return self._frame


class DataSource(Protocol):
    def get_adjusted_daily(self, symbol: str, start: date | None = None, end: date | None = None) -> AdjustedDailyBar:
        """Fetch adjusted daily OHLC data for symbol.

        Must return dataframe with columns: date (index), adj_open, adj_high, adj_low,
        adj_close, volume, open, high, low, close.
        """
        ...


class AbstractDataSource(abc.ABC):
    @abc.abstractmethod
    def get_adjusted_daily(self, symbol: str, start: date | None = None, end: date | None = None) -> AdjustedDailyBar:
        raise NotImplementedError
