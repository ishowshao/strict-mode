from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from strictmode import cli
from strictmode.engine.broker_ib import OrderResponse


class StubDataSource:
    def get_adjusted_daily(self, symbol: str):
        dates = pd.date_range("2023-01-01", periods=10, freq="D")
        data = {
            "open": [100 + i for i in range(10)],
            "high": [101 + i for i in range(10)],
            "low": [99 + i for i in range(10)],
            "close": [100.5 + i for i in range(10)],
            "adj_open": [200 + i for i in range(10)],
            "adj_high": [201 + i for i in range(10)],
            "adj_low": [199 + i for i in range(10)],
            "adj_close": [200.5 + i for i in range(10)],
            "volume": [1000] * 10,
        }
        return pd.DataFrame(data, index=dates)


class StubBroker:
    def __init__(self) -> None:
        self.orders = []

    def place_order(self, request):
        self.orders.append(request)
        return OrderResponse(order_id=len(self.orders), status="FILLED", description="stub")


class StubSettings:
    def __init__(self, db_url: str) -> None:
        self.database_url = db_url
        self.strategy = type("Strategy", (), {"atr_n": 3, "atr_k": 2.0, "drawdown_pct": None})()
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
    assert container.journal.get_position("TEST") is not None
    assert container.journal.get_stop("TEST") is not None

    result_dup = runner.invoke(cli.app, ["buy", "TEST", "10", "--mkt", "--dry-run"])
    assert result_dup.exit_code != 0
    assert "already exists" in result_dup.output

    sell_result = runner.invoke(cli.app, ["sell-all", "TEST", "--mkt", "--dry-run"])
    assert sell_result.exit_code == 0, sell_result.output
    assert container.journal.get_position("TEST") is None
    assert container.journal.get_stop("TEST") is None
