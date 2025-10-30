from __future__ import annotations

from datetime import datetime, timedelta
from math import isnan
from typing import Any, Callable, Iterable, List, Optional, Sequence

__all__ = ["Series", "DataFrame", "concat", "date_range", "isna"]


def isna(value: Any) -> bool:
    if isinstance(value, float):
        return isnan(value)
    return value is None


def date_range(start: str, periods: int, freq: str = "D") -> list[datetime]:
    start_dt = datetime.fromisoformat(start)
    if freq != "D":
        raise NotImplementedError("Only daily frequency supported")
    return [start_dt + timedelta(days=i) for i in range(periods)]


class SeriesILoc:
    def __init__(self, series: "Series") -> None:
        self.series = series

    def __getitem__(self, idx: int):
        if idx < 0:
            idx += len(self.series)
        return self.series.data[idx]


class Series:
    def __init__(self, data: Iterable[Any], index: Optional[Sequence[Any]] = None, name: str | None = None) -> None:
        self.data = list(data)
        self.index = list(index) if index is not None else list(range(len(self.data)))
        if len(self.index) != len(self.data):
            raise ValueError("Index and data length mismatch")
        self.name = name

    def __len__(self) -> int:
        return len(self.data)

    def __iter__(self):
        return iter(self.data)

    def __getitem__(self, idx: int) -> Any:
        if idx < 0:
            idx += len(self.data)
        return self.data[idx]

    def __getattr__(self, item: str):  # pragma: no cover - fallback
        raise AttributeError(item)

    @property
    def iloc(self) -> SeriesILoc:
        return SeriesILoc(self)

    def _binary_op(self, other: Any, op: Callable[[float, float], float]) -> "Series":
        if isinstance(other, Series):
            other_values = other.data
        else:
            other_values = [other] * len(self)
        result = []
        for a, b in zip(self.data, other_values):
            if isna(a) or isna(b):
                result.append(float("nan"))
            else:
                result.append(op(float(a), float(b)))
        return Series(result, index=self.index, name=self.name)

    def __sub__(self, other: Any) -> "Series":
        return self._binary_op(other, lambda a, b: a - b)

    def __add__(self, other: Any) -> "Series":
        return self._binary_op(other, lambda a, b: a + b)

    def __mul__(self, other: Any) -> "Series":
        return self._binary_op(other, lambda a, b: a * b)

    def __rmul__(self, other: Any) -> "Series":
        return self.__mul__(other)

    def abs(self) -> "Series":
        return Series([abs(float(v)) if not isna(v) else float("nan") for v in self.data], index=self.index, name=self.name)

    def shift(self, periods: int = 1) -> "Series":
        if periods < 0:
            raise NotImplementedError("Negative shift not supported")
        shifted = [float("nan")] * periods + self.data[:-periods] if periods else list(self.data)
        return Series(shifted, index=self.index, name=self.name)

    def rolling(self, window: int, min_periods: Optional[int] = None) -> "RollingSeries":
        return RollingSeries(self, window, min_periods or window)

    def combine(self, other: "Series", func: Callable[[Any, Any], Any]) -> "Series":
        result = []
        for a, b in zip(self.data, other.data):
            if isna(a) and isna(b):
                result.append(float("nan"))
            elif isna(a):
                result.append(b)
            elif isna(b):
                result.append(a)
            else:
                result.append(func(a, b))
        return Series(result, index=self.index, name=self.name)

    def cummax(self) -> "Series":
        current: float | None = None
        values: list[float] = []
        for value in self.data:
            if isna(value):
                values.append(float("nan"))
                continue
            value = float(value)
            if current is None or value > current:
                current = value
            values.append(current)
        return Series(values, index=self.index, name=self.name)

    def dropna(self) -> "Series":
        data: list[Any] = []
        index: list[Any] = []
        for idx, value in zip(self.index, self.data):
            if not isna(value):
                index.append(idx)
                data.append(value)
        return Series(data, index=index, name=self.name)

    def tail(self, count: int) -> "Series":
        return Series(self.data[-count:], index=self.index[-count:], name=self.name)


class RollingSeries:
    def __init__(self, series: Series, window: int, min_periods: int) -> None:
        self.series = series
        self.window = window
        self.min_periods = min_periods

    def _window_values(self, end: int) -> list[float]:
        start = max(0, end - self.window + 1)
        return [self.series.data[i] for i in range(start, end + 1) if not isna(self.series.data[i])]

    def mean(self) -> Series:
        values: list[float] = []
        for i in range(len(self.series)):
            window_vals = self._window_values(i)
            if len(window_vals) < self.min_periods:
                values.append(float("nan"))
            else:
                values.append(sum(float(v) for v in window_vals) / len(window_vals))
        return Series(values, index=self.series.index, name=self.series.name)

    def max(self) -> Series:
        values: list[float] = []
        for i in range(len(self.series)):
            window_vals = self._window_values(i)
            if len(window_vals) < self.min_periods:
                values.append(float("nan"))
            else:
                values.append(max(float(v) for v in window_vals))
        return Series(values, index=self.series.index, name=self.series.name)


class Row:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class DataFrameILoc:
    def __init__(self, df: "DataFrame") -> None:
        self.df = df

    def __getitem__(self, idx: int) -> Row:
        if idx < 0:
            idx += len(self.df)
        return Row({name: series.data[idx] for name, series in self.df._data.items()})


class DataFrame:
    def __init__(self, data: Optional[dict[str, Iterable[Any]]] = None, index: Optional[Sequence[Any]] = None) -> None:
        self._data: dict[str, Series] = {}
        self._index: list[Any] = []
        if data is not None:
            columns = {name: list(values) for name, values in data.items()}
            lengths = {len(values) for values in columns.values()}
            if len(lengths) > 1:
                raise ValueError("Column lengths must match")
            length = lengths.pop() if lengths else 0
            self._index = list(index) if index is not None else list(range(length))
            for name, values in columns.items():
                self._data[name] = Series(values, index=self._index, name=name)

    @classmethod
    def from_records(cls, records: Sequence[dict[str, Any]]) -> "DataFrame":
        if not records:
            return cls()
        columns: dict[str, list[Any]] = {key: [] for key in records[0].keys()}
        for record in records:
            for key, value in record.items():
                columns[key].append(value)
        return cls(columns)

    def set_index(self, key: str) -> "DataFrame":
        index_series = self._data.pop(key)
        new_index = list(index_series.data)
        columns = {name: series.data for name, series in self._data.items()}
        return DataFrame(columns, index=new_index)

    def sort_index(self) -> "DataFrame":
        paired = list(zip(self.index, range(len(self.index))))
        paired.sort(key=lambda item: item[0])
        order = [pos for _, pos in paired]
        columns = {}
        for name, series in self._data.items():
            columns[name] = [series.data[i] for i in order]
        new_index = [self.index[i] for i in order]
        return DataFrame(columns, index=new_index)

    def tail(self, count: int) -> "DataFrame":
        columns = {name: series.data[-count:] for name, series in self._data.items()}
        new_index = self.index[-count:]
        return DataFrame(columns, index=new_index)

    def __getitem__(self, key: str) -> Series:
        return self._data[key]

    def __setitem__(self, key: str, values: Iterable[Any]) -> None:
        self._data[key] = Series(list(values), index=self.index, name=key)

    def __len__(self) -> int:
        return len(self.index)

    @property
    def index(self) -> list[Any]:
        if not self._index and self._data:
            self._index = next(iter(self._data.values())).index
        return list(self._index)

    @index.setter
    def index(self, values: Sequence[Any]) -> None:
        self._index = list(values)
        for series in self._data.values():
            series.index = self._index

    @property
    def iloc(self) -> DataFrameILoc:
        return DataFrameILoc(self)

    def max(self, axis: int = 0) -> Series:
        if axis != 1:
            raise NotImplementedError("Only axis=1 supported")
        values: list[float] = []
        for i in range(len(self.index)):
            row = []
            for series in self._data.values():
                value = series.data[i]
                if not isna(value):
                    row.append(float(value))
            values.append(max(row) if row else float("nan"))
        return Series(values, index=self.index, name="max")


def concat(items: Sequence[Series], axis: int = 0) -> DataFrame:
    if axis != 1:
        raise NotImplementedError("Only axis=1 supported")
    columns = {}
    index = items[0].index if items else []
    for idx, series in enumerate(items):
        name = series.name or f"col{idx}"
        columns[name] = series.data
    return DataFrame(columns, index=index)
