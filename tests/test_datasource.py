from __future__ import annotations

from datetime import date

import pandas as pd

from strictmode.datasrc.av import AlphaVantageDataSource


class MockAlphaVantage(AlphaVantageDataSource):
    def __init__(self) -> None:
        super().__init__(api_key="demo")

    def _request(self, symbol: str):  # type: ignore[override]
        return {
            "Time Series (Daily)": {
                "2023-01-02": {
                    "1. open": "100",
                    "2. high": "110",
                    "3. low": "90",
                    "4. close": "105",
                    "5. adjusted close": "210",
                    "6. volume": "1000",
                },
                "2023-01-03": {
                    "1. open": "105",
                    "2. high": "115",
                    "3. low": "95",
                    "4. close": "110",
                    "5. adjusted close": "220",
                    "6. volume": "1200",
                },
            }
        }


def test_adjusted_fields_scaled():
    ds = MockAlphaVantage()
    df = ds.get_adjusted_daily("TEST")
    first = df.iloc[0]
    ratio = first["adj_close"] / first["close"]
    assert first["adj_open"] == first["open"] * ratio
    assert first["adj_high"] == first["high"] * ratio
    assert first["adj_low"] == first["low"] * ratio
