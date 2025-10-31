from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from strictmode.datasrc.base import AdjustedDailyBar
from strictmode.engine.broker_ib import OrderResponse
from strictmode.engine.daily_task import daily_update_task
from strictmode.engine.journal import Journal, Position, Stop


class StubDataSource:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def get_adjusted_daily(self, symbol: str):
        frame = self.frames[symbol]
        bars = AdjustedDailyBar(frame)
        bars.set_symbol(symbol)
        return bars


class StubBroker:
    def __init__(self) -> None:
        self.stop_orders: dict[str, list[tuple[int, float]]] = {}
        self.modified: list[tuple[int, float | None]] = []
        self.cancelled: list[int] = []

    def find_stop_orders(self, symbol: str):
        return self.stop_orders.get(symbol, [])

    def modify_order(self, order_id: int, stop_price: float | None = None, limit_price: float | None = None):
        self.modified.append((order_id, stop_price))
        return OrderResponse(order_id=order_id, status="UPDATED", description="stub")

    def cancel_order(self, order_id: int):
        self.cancelled.append(order_id)


class StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str, **_: object) -> None:
        self.messages.append(text)


class StubContainer:
    def __init__(self, journal: Journal, settings, broker: StubBroker, data_source: StubDataSource, notifier):
        self.journal = journal
        self.settings = settings
        self._broker = broker
        self._data_source = data_source
        self.notifier = notifier

    def data_source(self):
        return self._data_source

    def broker(self, paper: bool, dry_run: bool):  # noqa: ARG002
        return self._broker


def _settings(auto_liquidate: bool = False, drawdown_pct: float | None = None):
    return SimpleNamespace(
        tz_market="America/New_York",
        strategy=SimpleNamespace(
            atr_n=3,
            atr_k=2.0,
            auto_liquidate=auto_liquidate,
            drawdown_pct=drawdown_pct,
            rth_only=True,
        ),
    )


def _frame(values: list[float], start: str = "2023-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(values), freq="D")
    data = {
        "open": values,
        "high": [v + 2 for v in values],
        "low": [v - 2 for v in values],
        "close": values,
        "adj_open": values,
        "adj_high": [v + 2 for v in values],
        "adj_low": [v - 2 for v in values],
        "adj_close": values,
        "volume": [1000] * len(values),
    }
    return pd.DataFrame(data, index=idx)


def test_daily_task_updates_stop(monkeypatch):
    symbol = "TEST"
    frame = _frame([100, 102, 105, 110, 115])
    target_day = frame.index[-1].date()

    data_source = StubDataSource({symbol: frame})
    broker = StubBroker()
    broker.stop_orders[symbol] = [(1, 95.0)]
    notifier = StubNotifier()
    journal = Journal("sqlite:///:memory:")
    journal.upsert_position(Position(symbol=symbol, qty=10, avg_price=100.0, opened_at=datetime.utcnow(), paper=True))
    journal.upsert_stop(Stop(symbol=symbol, stop_price=95.0, method="chandelier", atr_n=3, atr_k=2.0, updated_at=datetime.utcnow()))

    container = StubContainer(journal, _settings(), broker, data_source, notifier)
    monkeypatch.setattr("strictmode.engine.daily_task._get_market_date", lambda settings: target_day)

    daily_update_task(container)

    updated_stop = journal.get_stop(symbol)
    assert updated_stop is not None and updated_stop.stop_price > 95.0
    assert broker.modified and broker.modified[0][1] == pytest.approx(updated_stop.stop_price)
    assert notifier.messages and any("Daily Update Summary" in msg for msg in notifier.messages)


def test_daily_task_triggers_notification(monkeypatch):
    symbol = "TEST"
    frame = _frame([100, 98, 94])
    target_day = frame.index[-1].date()

    data_source = StubDataSource({symbol: frame})
    broker = StubBroker()
    notifier = StubNotifier()
    journal = Journal("sqlite:///:memory:")
    journal.upsert_position(Position(symbol=symbol, qty=5, avg_price=100.0, opened_at=datetime.utcnow(), paper=True))
    journal.upsert_stop(Stop(symbol=symbol, stop_price=95.0, method="chandelier", atr_n=3, atr_k=2.0, updated_at=datetime.utcnow()))

    container = StubContainer(journal, _settings(auto_liquidate=False), broker, data_source, notifier)
    monkeypatch.setattr("strictmode.engine.daily_task._get_market_date", lambda settings: target_day)

    daily_update_task(container)

    assert any("🚨" in msg for msg in notifier.messages)
    # Stop remains to allow manual follow-up when only notification is sent
    assert journal.get_stop(symbol).stop_price == 95.0


def test_daily_task_skips_on_data_lag(monkeypatch):
    symbol = "TEST"
    frame = _frame([100, 101, 102])
    target_day = date(2023, 2, 1)

    data_source = StubDataSource({symbol: frame})
    broker = StubBroker()
    notifier = StubNotifier()
    journal = Journal("sqlite:///:memory:")
    journal.upsert_position(Position(symbol=symbol, qty=3, avg_price=100.0, opened_at=datetime.utcnow(), paper=True))
    journal.upsert_stop(Stop(symbol=symbol, stop_price=90.0, method="chandelier", atr_n=3, atr_k=2.0, updated_at=datetime.utcnow()))

    container = StubContainer(journal, _settings(), broker, data_source, notifier)
    monkeypatch.setattr("strictmode.engine.daily_task._get_market_date", lambda settings: target_day)

    daily_update_task(container)

    assert any("Data lag alert" in msg for msg in notifier.messages)
    assert journal.get_stop(symbol).stop_price == 90.0
