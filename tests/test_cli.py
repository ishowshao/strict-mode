from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from strictmode import cli
from strictmode.datasrc.base import AdjustedDailyBar
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
        df = pd.DataFrame(data, index=self.dates)
        adj_bar = AdjustedDailyBar(df)
        adj_bar.set_symbol(symbol)
        return adj_bar


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

    def list_open_orders(self):  # minimal shape used by reconcile-stops
        # Build rows from any staged orders
        rows = []
        for idx, r in enumerate(self.orders, start=1):
            rows.append(
                {
                    "orderId": idx,
                    "symbol": r.symbol,
                    "type": r.order_type,
                    "orderRef": getattr(r, "order_ref", f"SM:{r.symbol}"),
                    "totalQuantity": r.qty,
                    "auxPrice": r.stop_price,
                    "lmtPrice": r.limit_price,
                    "parentId": r.parent_id,
                    "tif": r.tif,
                    "action": r.side,
                    "status": "Submitted",
                }
            )
        return rows

    def list_completed_orders(self, api_only: bool = True):  # noqa: ARG002
        # Provide one filled and one cancelled for CLI tests
        return [
            {
                "orderId": 1001,
                "symbol": "TEST",
                "type": "LMT",
                "orderRef": "SM:TEST",
                "totalQuantity": 5,
                "auxPrice": None,
                "lmtPrice": 101.0,
                "parentId": None,
                "tif": "DAY",
                "action": "BUY",
                "status": "Filled",
            },
            {
                "orderId": 1002,
                "symbol": "TEST",
                "type": "STP",
                "orderRef": "SM:TEST",
                "totalQuantity": 5,
                "auxPrice": 95.0,
                "lmtPrice": None,
                "parentId": 1000,
                "tif": "GTC",
                "action": "SELL",
                "status": "Cancelled",
            },
        ]


class StubSettings:
    def __init__(self, db_url: str) -> None:
        self.database_url = db_url
        self.tz_market = "America/New_York"
        self.strategy = type(
            "Strategy",
            (),
            {"atr_n": 3, "atr_k": 2.0, "drawdown_pct": None, "initial_stop_pct": 0.05},
        )()
        self.data = type("Data", (), {"api_key": None, "source": "yfinance"})()
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
    assert result_dup.exit_code == 0, result_dup.output
    # Multiple BUYs allowed; position aggregates quantity and weighted avg
    position2 = container.journal.get_position("TEST")
    assert position2 is not None and position2.qty == pytest.approx(20)
    # Since both buys use the same latest close for fill, avg unchanged
    assert position2.avg_price == pytest.approx(position.avg_price)
    # DB stop should not be lowered; equals  (avg_price * (1 - pct)) in this deterministic stub
    stop_record2 = container.journal.get_stop("TEST")
    assert stop_record2 is not None
    assert stop_record2.stop_price == pytest.approx(position2.avg_price * (1 - container.settings.strategy.initial_stop_pct))
    # Dry-run broker captured four orders (2x parent + 2x stop)
    assert len(container._broker.orders) == 4

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


def test_reconcile_stops_dry_run(monkeypatch, runner, tmp_path):
    container = StubContainer(tmp_path)
    # Seed a position of 10 shares
    container.journal.upsert_position(
        cli.Position(symbol="TEST", qty=10, avg_price=100.0, opened_at=cli.datetime.utcnow(), paper=True)
    )
    # Seed two STOP orders for 12 shares total
    container._broker.orders.append(
        cli.OrderRequest(symbol="TEST", qty=5, side="SELL", order_type="STP", stop_price=95.0)
    )
    container._broker.orders.append(
        cli.OrderRequest(symbol="TEST", qty=7, side="SELL", order_type="STP", stop_price=96.0)
    )
    # Ensure reconcile sees STOPs even without real IB backend
    container._broker.list_open_orders = lambda: [
        {
            "orderId": 1,
            "symbol": "TEST",
            "type": "STP",
            "orderRef": "SM:TEST",
            "totalQuantity": 5,
            "auxPrice": 95.0,
            "lmtPrice": None,
        },
        {
            "orderId": 2,
            "symbol": "TEST",
            "type": "STP",
            "orderRef": "SM:TEST",
            "totalQuantity": 7,
            "auxPrice": 96.0,
            "lmtPrice": None,
        },
    ]

    monkeypatch.setattr(cli, "build_container", lambda: container)
    res = runner.invoke(cli.app, ["reconcile-stops", "TEST", "--dry-run"])  # preview only
    assert res.exit_code == 0, res.output
    assert "total STOP qty" in res.output
    assert "Plan to cancel" in res.output
    assert "Dry-run/preview only" in res.output


def test_show_orders_filters(monkeypatch, runner, tmp_path):
    container = StubContainer(tmp_path)
    # Add two open orders
    container._broker.orders.append(
        cli.OrderRequest(symbol="TEST", qty=1, side="BUY", order_type="LMT", limit_price=100.0)
    )
    container._broker.orders.append(
        cli.OrderRequest(symbol="TEST", qty=1, side="SELL", order_type="STP", stop_price=95.0)
    )
    monkeypatch.setattr(cli, "build_container", lambda: container)

    # Default: only live
    res_open = runner.invoke(cli.app, ["show-orders"])  # state=live
    assert res_open.exit_code == 0
    assert "status=Submitted" in res_open.output
    assert "status=Filled" not in res_open.output

    # Only completed
    res_completed = runner.invoke(cli.app, ["show-orders", "--state", "completed"])
    assert res_completed.exit_code == 0
    assert "status=Filled" in res_completed.output
    assert "status=Cancelled" not in res_completed.output

    # Only cancelled
    res_cancelled = runner.invoke(cli.app, ["show-orders", "--state", "cancelled"])
    assert res_cancelled.exit_code == 0
    assert "status=Cancelled" in res_cancelled.output


def test_cancel_cli(monkeypatch, runner, tmp_path):
    container = StubContainer(tmp_path)
    # Create live parent+child chain and one unrelated order
    def _open_rows():
        return [
            {
                "orderId": 1,
                "symbol": "TEST",
                "type": "LMT",
                "orderRef": "SM:TEST",
                "totalQuantity": 5,
                "lmtPrice": 100.0,
                "auxPrice": None,
                "parentId": None,
                "tif": "DAY",
                "action": "BUY",
                "status": "Submitted",
            },
            {
                "orderId": 2,
                "symbol": "TEST",
                "type": "STP",
                "orderRef": "SM:TEST",
                "totalQuantity": 5,
                "lmtPrice": None,
                "auxPrice": 95.0,
                "parentId": 1,
                "tif": "GTC",
                "action": "SELL",
                "status": "Submitted",
            },
            {
                "orderId": 99,
                "symbol": "XYZ",
                "type": "LMT",
                "orderRef": "SM:XYZ",
                "totalQuantity": 1,
                "lmtPrice": 10.0,
                "auxPrice": None,
                "parentId": None,
                "tif": "DAY",
                "action": "BUY",
                "status": "Submitted",
            },
        ]

    container._broker.list_open_orders = _open_rows
    monkeypatch.setattr(cli, "build_container", lambda: container)

    # Preview by id: should include child 2
    res_preview = runner.invoke(cli.app, ["cancel", "--id", "1", "--dry-run"])
    assert res_preview.exit_code == 0
    assert "Plan to cancel 2 order(s): [1, 2]" in res_preview.output

    # Apply by symbol: cancel both 1 and 2, not 99
    res_apply = runner.invoke(cli.app, ["cancel", "--symbol", "TEST", "--apply"])
    assert res_apply.exit_code == 0
    assert 1 in container._broker.cancelled and 2 in container._broker.cancelled
    assert 99 not in container._broker.cancelled
