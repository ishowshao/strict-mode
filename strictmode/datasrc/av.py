from __future__ import annotations

from datetime import date
from functools import cached_property
from typing import Any

import httpx
import pandas as pd

from .base import AbstractDataSource, AdjustedDailyBar


class AlphaVantageDataSource(AbstractDataSource):
    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str, session: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self._session = session

    @cached_property
    def session(self) -> httpx.Client:
        if self._session is not None:
            return self._session
        return httpx.Client(timeout=30.0)

    def _build_params(self, symbol: str) -> dict[str, str]:
        params = {
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": symbol,
            "outputsize": "compact",
            "apikey": self.api_key,
        }
        return params

    def _request(self, symbol: str) -> dict[str, Any]:
        response = self.session.get(self.BASE_URL, params=self._build_params(symbol))
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if "Error Message" in payload:
            raise RuntimeError(payload["Error Message"])
        return payload

    def get_adjusted_daily(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> AdjustedDailyBar:
        payload = self._request(symbol)
        timeseries = payload.get("Time Series (Daily)")
        if timeseries is None:
            raise RuntimeError("Unexpected Alpha Vantage response: missing time series")
        records: list[dict[str, Any]] = []
        for day_str, row in timeseries.items():
            day = date.fromisoformat(day_str)
            if start and day < start:
                continue
            if end and day > end:
                continue
            close = float(row["4. close"])
            adj_close = float(row["5. adjusted close"])
            ratio = adj_close / close if close else 0.0
            open_price = float(row["1. open"])
            high = float(row["2. high"])
            low = float(row["3. low"])
            records.append(
                {
                    "date": day,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "adj_open": open_price * ratio,
                    "adj_high": high * ratio,
                    "adj_low": low * ratio,
                    "adj_close": adj_close,
                    "volume": float(row["6. volume"]),
                    "ratio": ratio,
                }
            )
        if not records:
            raise RuntimeError("No data returned for symbol")
        df = pd.DataFrame.from_records(records).set_index("date").sort_index()
        adj_df = AdjustedDailyBar(df)
        adj_df.set_symbol(symbol)
        return adj_df

    def close(self) -> None:
        if self._session is None and "session" in self.__dict__:
            self.session.close()
