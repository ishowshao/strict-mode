from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from strictmode import cli
from strictmode.engine.broker_ib import OrderResponse


class StubDataSource:
    def __init__(self, start: str = "2023-01-01", periods: int = 10, freq: str = "D") -> None:
        self.dates = pd.date_range(start, periods=periods, freq=freq)

    def get_adjusted_daily(self, symbol: str):
        data = {
            "open": [100 + i for i in range(len(self.dates))],
            "high": [101 + i for i in range(len(self.dates))],
            "low": [99 + i for i in range(len(self.dates))],
            "close": [100.5 + i for i in range(len(self.dates))],
            "adj_open": [200 + i for i in range(len(self.dates))],
            "adj_high": [201 + i for i in range(len(self.dates))],
            "adj_low": [199 + i for i in range(len(self.dates))],
            "adj_close": [200.5 + i for i in range(len(self.dates))],
            "volume": [1000] * len(self.dates),
        }
        return pd.DataFrame(data, index=self.dates)


class StubBroker:
    def __init__(self) -> None:
        self.orders = []
        self.modified: list[tuple[int | None, float | None]] = []
        self.cancelled: list[int] = []

    def place_order(self, request):
        self.orders.append(request)
        return OrderResponse(order_id=len(self.orders), status="FILLED", description="stub")

    def find_stop_orders(self, symbol: str):
        return []

    def cancel_order(self, order_id: int):
        self.cancelled.append(order_id)

    def modify_order(self, order_id: int, stop_price: float | None = None, limit_price: float | None = None):
        self.modified.append((order_id, stop_price))
        return OrderResponse(order_id=order_id, status="UPDATED", description="stub")


class StubSettings:
    def __init__(self, db_url: str) -> None:
        self.database_url = db_url
        self.tz_market = "America/New_York"
        self.strategy = type(
            "Strategy",
            (),
            {"atr_n": 3, "atr_k": 2.0, "drawdown_pct": None, "initial_stop_pct": 0.05},
        )()
        self.data = type("Data", (), {"api_key": "demo"})()
        self.ib = type("IB", (), {"host": "", "port": 0, "client_id": 0})()
        self.telegram = None


class StubContainer:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = StubSettings(f"sqlite:///{tmp_path/'test.db'}")
        self.journal = cli.Journal(self.settings.database_url)
        self.notifier = None
        self._data_source = StubDataSource()
        self._broker = StubBroker()

    def data_source(self):
        return self._data_source

    def broker(self, paper: bool, dry_run: bool):
        return self._broker


@pytest.fixture
def runner():
    return CliRunner()


def test_buy_and_sell_cli(monkeypatch, runner, tmp_path):
    container = StubContainer(tmp_path)
    monkeypatch.setattr(cli, "build_container", lambda: container)

    result = runner.invoke(cli.app, ["buy", "TEST", "10", "--mkt", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(container._broker.orders) == 2
    position = container.journal.get_position("TEST")
    stop_record = container.journal.get_stop("TEST")
    assert position is not None
    assert stop_record is not None
    assert stop_record.stop_price == pytest.approx(position.avg_price * (1 - container.settings.strategy.initial_stop_pct))

    result_dup = runner.invoke(cli.app, ["buy", "TEST", "10", "--mkt", "--dry-run"])
    assert result_dup.exit_code != 0
    assert "already exists" in result_dup.output

    sell_result = runner.invoke(cli.app, ["sell-all", "TEST", "--mkt", "--dry-run"])
    assert sell_result.exit_code == 0, sell_result.output
    assert container.journal.get_position("TEST") is None
    assert container.journal.get_stop("TEST") is None


def test_sync_and_show_data_cli(monkeypatch, runner, tmp_path):
    container = StubContainer(tmp_path)
    container._data_source = StubDataSource(start="2023-01-01", periods=15)
    monkeypatch.setattr(cli, "build_container", lambda: container)
    monkeypatch.setattr(cli, "_market_date", lambda settings: date(2023, 1, 15))

    result = runner.invoke(cli.app, ["sync-data", "TEST", "--days", "5"])
    assert result.exit_code == 0, result.output
    assert "Cached 5 rows for TEST" in result.output

    cached = container.journal.list_cached_prices("TEST")
    assert len(cached) == 5
    assert cached[0]["date"] == date(2023, 1, 15)

    show_result = runner.invoke(cli.app, ["show-data", "TEST", "--limit", "3", "--ascending"])
    assert show_result.exit_code == 0, show_result.output
    rows = [line for line in show_result.output.splitlines() if "open=" in line]
    assert len(rows) == 3
    assert rows[0].startswith("2023-01-11")


def test_sync_data_days_limit(monkeypatch, runner):
    result = runner.invoke(cli.app, ["sync-data", "TEST", "--days", "120"])
    assert result.exit_code != 0
    assert "Maximum allowed window is 90 days." in result.output
