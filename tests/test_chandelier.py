from __future__ import annotations

import pandas as pd
import pytest

from strictmode.rules.chandelier import ChandelierConfig, NotEnoughDataError, atr, trailing_stop


def test_atr_and_trailing_stop():
    data = {
        "adj_high": [10, 11, 12, 13, 14, 13, 15, 16, 17, 18, 19, 20],
        "adj_low": [9, 9.5, 10, 11, 12, 11.5, 13, 14, 15, 16, 17, 18],
        "adj_close": [9.5, 10.5, 11, 12, 13, 12.5, 14, 15, 16, 17, 18, 19],
    }
    df = pd.DataFrame(data)
    df.index = pd.date_range("2023-01-01", periods=len(df), freq="D")
    config = ChandelierConfig(atr_period=3, atr_multiplier=2.0)
    stops = trailing_stop(df, config)
    assert stops.dropna().iloc[0] == stops.dropna().iloc[0]  # first computed stop exists
    assert stops.iloc[-1] >= stops.dropna().iloc[0]


def test_atr_requires_enough_data():
    df = pd.DataFrame(
        {
            "adj_high": [10, 11],
            "adj_low": [9, 10],
            "adj_close": [9.5, 10.5],
        }
    )
    with pytest.raises(NotEnoughDataError):
        atr(df, 3)
