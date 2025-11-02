from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from strictmode.datasrc.av import AlphaVantageDataSource
from strictmode.datasrc.yfinance import YFinanceDataSource


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


def create_mock_yfinance_dataframe() -> pd.DataFrame:
    """Create a mock yfinance DataFrame for testing."""
    data = {
        "Open": [100.0, 105.0],
        "High": [110.0, 115.0],
        "Low": [90.0, 95.0],
        "Close": [105.0, 110.0],
        "Adj Close": [210.0, 220.0],
        "Volume": [1000, 1200],
    }
    index = pd.date_range("2023-01-02", periods=2, freq="D")
    df = pd.DataFrame(data, index=index)
    return df


class MockYFinanceTicker:
    def __init__(self, symbol: str, hist_data: pd.DataFrame | None = None) -> None:
        self.symbol = symbol
        self._hist_data = hist_data if hist_data is not None else create_mock_yfinance_dataframe()

    def history(self, start=None, end=None, auto_adjust=False) -> pd.DataFrame:
        return self._hist_data.copy()


@pytest.mark.parametrize("data_source_class", [MockAlphaVantage, YFinanceDataSource])
def test_adjusted_fields_scaled(data_source_class):
    """Test that adjusted fields are correctly scaled for both data sources."""
    if data_source_class == MockAlphaVantage:
        ds = MockAlphaVantage()
        df = ds.get_adjusted_daily("TEST")
    else:
        # Mock yfinance for YFinanceDataSource
        mock_ticker = MockYFinanceTicker("TEST")
        with patch("strictmode.datasrc.yfinance.yf.Ticker", return_value=mock_ticker):
            ds = YFinanceDataSource()
            df = ds.get_adjusted_daily("TEST")

    first = df.iloc[0]
    ratio = first["adj_close"] / first["close"]
    assert first["adj_open"] == first["open"] * ratio
    assert first["adj_high"] == first["high"] * ratio
    assert first["adj_low"] == first["low"] * ratio


def test_yfinance_empty_dataframe():
    """Test that YFinanceDataSource raises RuntimeError for empty DataFrame."""
    mock_ticker = MockYFinanceTicker("TEST", hist_data=pd.DataFrame())
    with patch("strictmode.datasrc.yfinance.yf.Ticker", return_value=mock_ticker):
        ds = YFinanceDataSource()
        with pytest.raises(RuntimeError, match="No data returned"):
            ds.get_adjusted_daily("TEST")


def test_yfinance_zero_close_prices():
    """Test that YFinanceDataSource handles zero close prices correctly."""
    data = {
        "Open": [100.0, 105.0],
        "High": [110.0, 115.0],
        "Low": [90.0, 95.0],
        "Close": [0.0, 110.0],  # First close is zero
        "Adj Close": [0.0, 220.0],
        "Volume": [1000, 1200],
    }
    index = pd.date_range("2023-01-02", periods=2, freq="D")
    hist_data = pd.DataFrame(data, index=index)

    mock_ticker = MockYFinanceTicker("TEST", hist_data=hist_data)
    with patch("strictmode.datasrc.yfinance.yf.Ticker", return_value=mock_ticker):
        ds = YFinanceDataSource()
        df = ds.get_adjusted_daily("TEST")
        # Should only return the row with non-zero close
        assert len(df) == 1
        assert df.iloc[0]["close"] == 110.0


def test_yfinance_all_zero_close_prices():
    """Test that YFinanceDataSource raises RuntimeError when all close prices are zero."""
    data = {
        "Open": [100.0, 105.0],
        "High": [110.0, 115.0],
        "Low": [90.0, 95.0],
        "Close": [0.0, 0.0],  # All closes are zero
        "Adj Close": [0.0, 0.0],
        "Volume": [1000, 1200],
    }
    index = pd.date_range("2023-01-02", periods=2, freq="D")
    hist_data = pd.DataFrame(data, index=index)

    mock_ticker = MockYFinanceTicker("TEST", hist_data=hist_data)
    with patch("strictmode.datasrc.yfinance.yf.Ticker", return_value=mock_ticker):
        ds = YFinanceDataSource()
        with pytest.raises(RuntimeError, match="No usable close prices"):
            ds.get_adjusted_daily("TEST")


def test_yfinance_date_filtering():
    """Test that YFinanceDataSource correctly filters by start and end dates."""
    # Create data spanning 5 days
    data = {
        "Open": [100.0, 105.0, 110.0, 115.0, 120.0],
        "High": [110.0, 115.0, 120.0, 125.0, 130.0],
        "Low": [90.0, 95.0, 100.0, 105.0, 110.0],
        "Close": [105.0, 110.0, 115.0, 120.0, 125.0],
        "Adj Close": [210.0, 220.0, 230.0, 240.0, 250.0],
        "Volume": [1000, 1200, 1300, 1400, 1500],
    }
    index = pd.date_range("2023-01-02", periods=5, freq="D")
    hist_data = pd.DataFrame(data, index=index)

    mock_ticker = MockYFinanceTicker("TEST", hist_data=hist_data)
    with patch("strictmode.datasrc.yfinance.yf.Ticker", return_value=mock_ticker):
        ds = YFinanceDataSource()
        # Filter to middle 3 days
        start_date = date(2023, 1, 3)
        end_date = date(2023, 1, 5)
        df = ds.get_adjusted_daily("TEST", start=start_date, end=end_date)
        assert len(df) == 3
        assert df.index[0] == start_date
        assert df.index[-1] == end_date


def test_yfinance_date_filtering_empty():
    """Test that YFinanceDataSource raises RuntimeError when date filtering results in empty data."""
    data = {
        "Open": [100.0, 105.0],
        "High": [110.0, 115.0],
        "Low": [90.0, 95.0],
        "Close": [105.0, 110.0],
        "Adj Close": [210.0, 220.0],
        "Volume": [1000, 1200],
    }
    index = pd.date_range("2023-01-02", periods=2, freq="D")
    hist_data = pd.DataFrame(data, index=index)

    mock_ticker = MockYFinanceTicker("TEST", hist_data=hist_data)
    with patch("strictmode.datasrc.yfinance.yf.Ticker", return_value=mock_ticker):
        ds = YFinanceDataSource()
        # Filter to a date range that doesn't overlap
        start_date = date(2024, 1, 1)
        end_date = date(2024, 1, 31)
        with pytest.raises(RuntimeError, match="No data returned"):
            ds.get_adjusted_daily("TEST", start=start_date, end=end_date)


def test_yfinance_network_error():
    """Test that YFinanceDataSource converts network errors to RuntimeError."""
    with patch("strictmode.datasrc.yfinance.yf.Ticker", side_effect=Exception("Network error")):
        ds = YFinanceDataSource()
        with pytest.raises(RuntimeError, match="Failed to fetch data"):
            ds.get_adjusted_daily("TEST")


def test_yfinance_sets_symbol():
    """Test that YFinanceDataSource sets the symbol correctly."""
    mock_ticker = MockYFinanceTicker("AAPL")
    with patch("strictmode.datasrc.yfinance.yf.Ticker", return_value=mock_ticker):
        ds = YFinanceDataSource()
        df = ds.get_adjusted_daily("AAPL")
        assert df.symbol == "AAPL"
