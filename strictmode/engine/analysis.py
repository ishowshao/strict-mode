from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

import pandas as pd

from ..rules.chandelier import ChandelierConfig, atr as _atr, chandelier_exit as _chandelier_exit


@dataclass(slots=True)
class ChandelierTableResult:
    symbol: str
    entry_date: pd.Timestamp
    config: ChandelierConfig
    table: pd.DataFrame  # columns: adj_close, atr, chandelier, stop_trailing, delta_stop, n_from_entry


def _to_dataframe_from_cache(rows: Iterable[dict]) -> pd.DataFrame:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    # Reconstruct adjusted highs/lows consistently with data sources: ratio = adj_close / close
    ratio = df["adj_close"] / df["close"]
    df["adj_high"] = df["high"] * ratio
    df["adj_low"] = df["low"] * ratio
    return df


def build_chandelier_table(
    symbol: str,
    rows: Iterable[dict],
    config: ChandelierConfig,
    entry: Optional[date] = None,
    days: Optional[int] = None,
    initial_stop_pct: float = 0.05,
) -> ChandelierTableResult:
    """Build a verification table using cached price rows only.

    - Uses existing ATR/Chandelier implementations from strictmode.rules.chandelier
    - Trailing stop is computed from the chosen entry day (monotonic non-decreasing)
    - Returns rows strictly after entry day: n_from_entry = 1..N
    """
    df = _to_dataframe_from_cache(rows)
    if df.empty:
        raise RuntimeError("No cached data available")

    # Compute indicator series using canonical implementations
    atr_series = _atr(df, config.atr_period)
    chandelier = _chandelier_exit(df, config)

    first_valid_ts = chandelier.first_valid_index()
    if first_valid_ts is None:
        # Not enough data for ATR/chandelier
        raise RuntimeError(f"Insufficient data: need at least {config.atr_period} rows")

    # Determine entry timestamp on/after requested date, but not before first calculable bar
    if entry is None:
        entry_ts = pd.Timestamp(first_valid_ts)
    else:
        # Align to the next available trading day in cache
        requested = pd.Timestamp(entry)
        idx = df.index.searchsorted(requested, side="left")
        if idx >= len(df.index):
            raise RuntimeError("Entry date is after last cached row")
        entry_ts = df.index[idx]
        if entry_ts < first_valid_ts:
            entry_ts = pd.Timestamp(first_valid_ts)

    # Build full output frame from the very first cached day to end
    full_index = df.index
    if len(full_index) == 0:
        raise RuntimeError("Empty cache")

    out = pd.DataFrame(index=full_index)
    out["adj_close"] = df.loc[full_index, "adj_close"]
    out["close"] = df.loc[full_index, "close"]
    out["atr"] = atr_series.loc[full_index]
    out["chandelier"] = chandelier.loc[full_index]

    # Compute n_from_entry relative to entry_ts
    # Map each index to an integer offset from entry index
    entry_pos = full_index.get_indexer([entry_ts])[0]
    offsets = [i - entry_pos for i in range(len(full_index))]
    out["n_from_entry"] = offsets

    # Compute stop_trailing: NA for n<0, initial % stop for n==0, trailing thereafter
    stop_vals: list[float | float("nan") | None] = []  # type: ignore[valid-type]
    last_stop: float | None = None
    initial_stop = float(out.loc[entry_ts, "close"]) * (1 - float(initial_stop_pct))
    for ts, row in out.iterrows():
        n = int(row["n_from_entry"])  # type: ignore[assignment]
        ch = float(row["chandelier"]) if pd.notna(row["chandelier"]) else float("nan")
        if n < 0:
            stop_vals.append(float("nan"))
            continue
        if n == 0:
            last_stop = initial_stop
            stop_vals.append(last_stop)
            continue
        # n >= 1
        if pd.isna(ch):
            # Shouldn't happen after first_valid_ts, but keep safety
            stop_vals.append(last_stop)
            continue
        last_stop = max(last_stop if last_stop is not None else ch, ch)
        stop_vals.append(last_stop)
    out["stop_trailing"] = pd.Series(stop_vals, index=out.index)
    out["delta_stop"] = out["stop_trailing"].diff()

    # Apply days limit: keep all rows up to entry (n<=0), plus first `days` after entry
    if days is not None and days > 0:
        pre = out[out["n_from_entry"] <= 0]
        post = out[out["n_from_entry"] >= 1].head(days)
        out = pd.concat([pre, post], axis=0)

    return ChandelierTableResult(symbol=symbol, entry_date=pd.Timestamp(entry_ts), config=config, table=out)
