from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from .base import AbstractDataSource, AdjustedDailyBar


class YFinanceDataSource(AbstractDataSource):
    """yfinance-based data source implementation."""

    def __init__(self, session: yf.Ticker | None = None) -> None:
        # yfinance 不需要 session，但保留接口兼容性
        pass

    def get_adjusted_daily(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> AdjustedDailyBar:
        """Fetch adjusted daily OHLC data for symbol using yfinance.

        Args:
            symbol: Ticker symbol
            start: Start date (inclusive)
            end: End date (inclusive)

        Returns:
            AdjustedDailyBar with columns: date (index), open, high, low, close,
            adj_open, adj_high, adj_low, adj_close, volume, ratio

        Raises:
            RuntimeError: If no data is returned or no usable close prices
        """
        # 转换日期格式
        if start:
            start_str = start.isoformat()
        else:
            default_start = date.today() - timedelta(days=120)
            start_str = default_start.isoformat()
        end_str = end.isoformat() if end else None

        try:
            # 使用 auto_adjust=False 获取原始数据，自行计算复权
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=start_str, end=end_str, auto_adjust=False)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch data from yfinance for {symbol}: {e}") from e

        if hist.empty:
            raise RuntimeError(f"No data returned for symbol {symbol}")

        # 列名标准化
        rename_map = {
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "adj_close",
            "Volume": "volume",
        }

        # 检查必需的列是否存在
        missing_cols = [col for col in rename_map.keys() if col not in hist.columns]
        if missing_cols:
            raise RuntimeError(
                f"Missing required columns from yfinance: {missing_cols}. "
                f"Available columns: {list(hist.columns)}"
            )

        # 重命名列并选择需要的列
        hist = hist.rename(columns=rename_map)
        hist = hist[list(rename_map.values())]

        # 处理日期索引：转换为 date 类型
        hist.index = pd.to_datetime(hist.index).normalize()
        hist.index.name = "date"
        hist.index = hist.index.date

        # 如果指定了日期范围，进行过滤
        if start:
            hist = hist[hist.index >= start]
        if end:
            hist = hist[hist.index <= end]

        if hist.empty:
            raise RuntimeError(f"No data returned for symbol {symbol} in the specified date range")

        # 计算复权比例
        # 将 close == 0 的行替换为缺失值，避免除零
        hist["close"] = hist["close"].replace(0, pd.NA)
        ratio = hist["adj_close"] / hist["close"]

        # 检查是否有可用的复权比例
        if ratio.isna().all():
            raise RuntimeError("No usable close prices returned from yfinance")

        # 计算复权价格
        hist["adj_open"] = hist["open"] * ratio
        hist["adj_high"] = hist["high"] * ratio
        hist["adj_low"] = hist["low"] * ratio
        hist["ratio"] = ratio

        # 对于缺失值，使用前向填充（如果前面有数据）
        # 如果第一行就有缺失，则删除该行
        # 删除 ratio 为 NaN 的行（包括零价格行）
        hist = hist.ffill().dropna(subset=["ratio"])

        if hist.empty:
            raise RuntimeError("No usable data after processing adjustments")

        # 确保数据按日期升序排列
        hist = hist.sort_index()

        # 构建返回对象
        adj_df = AdjustedDailyBar(hist)
        adj_df.set_symbol(symbol)
        return adj_df
