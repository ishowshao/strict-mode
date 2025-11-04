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


def test_buy_hk_defaults_currency(monkeypatch, runner, tmp_path):
    container = StubContainer(tmp_path)
    monkeypatch.setattr(cli, "build_container", lambda: container)

    res = runner.invoke(cli.app, ["buy", "9988.HK", "2", "--mkt", "--dry-run"])
    assert res.exit_code == 0, res.output
    # In dry-run, two orders are staged (parent + stop)
    assert len(container._broker.orders) == 2
    assert container._broker.orders[0].currency == "HKD"
    assert container._broker.orders[1].currency == "HKD"


def test_sell_hk_defaults_currency(monkeypatch, runner, tmp_path):
    container = StubContainer(tmp_path)
    # Seed an HK position
    from datetime import datetime, timezone
    container.journal.upsert_position(
        cli.Position(symbol="9988.HK", qty=3, avg_price=100.0, opened_at=datetime.now(timezone.utc), paper=True)
    )
    monkeypatch.setattr(cli, "build_container", lambda: container)

    res = runner.invoke(cli.app, ["sell-all", "9988.HK", "--mkt", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert len(container._broker.orders) == 1
    assert container._broker.orders[0].currency == "HKD"


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
    from datetime import datetime, timezone
    container.journal.upsert_position(
        cli.Position(symbol="TEST", qty=10, avg_price=100.0, opened_at=datetime.now(timezone.utc), paper=True)
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


def test_tick_size_cli(runner):
    res_hk = runner.invoke(cli.app, ["tick-size", "9988.HK", "154.85", "--mode", "nearest"])
    assert res_hk.exit_code == 0
    assert "exchange=SEHK" in res_hk.output
    # 154.85 with 0.1 tick -> 154.9 (nearest)
    assert "rounded=154.9" in res_hk.output

    res_us = runner.invoke(cli.app, ["tick-size", "AAPL", "256.857", "--mode", "down"])
    assert res_us.exit_code == 0
    assert "exchange=US/SMART" in res_us.output
    assert "rounded=256.85" in res_us.output


def test_chandelier_table_from_cache(monkeypatch, runner, tmp_path):
    # Prepare container and seed cache with deterministic bars
    container = StubContainer(tmp_path)
    monkeypatch.setattr(cli, "build_container", lambda: container)

    # Seed 15 days of cache: open=100+i, high=open+1, low=open-1, close=open+0.5, adj_close=close*2
    import datetime as _dt
    start = _dt.date(2023, 1, 1)
    for i in range(15):
        d = start + _dt.timedelta(days=i)
        o = 100 + i
        h = o + 1
        l = o - 1
        c = o + 0.5
        container.journal.cache_price_data(
            symbol="TEST",
            price_date=d,
            open_price=o,
            high=h,
            low=l,
            close=c,
            adj_close=c * 2.0,  # constant ratio 2x
        )
    # Use strategy defaults from StubSettings: atr_n=3, atr_k=2.0
    # Entry on 2023-01-05 -> should print days after entry
    res = runner.invoke(cli.app, [
        "chandelier-table",
        "TEST",
        "--entry",
        "2023-01-05",
        "--days",
        "5",
    ])
    assert res.exit_code == 0, res.output
    # Header present with initial stop percentage (5.0%)
    assert "TEST | ATR(n=3) k=2.0 init_stop=5.0% | entry=2023-01-05" in res.output
    lines = res.output.splitlines()
    # First calculable day with n=3 ATR is 2023-01-03 and should be present with '-' stop
    # n_from_entry for 2023-01-03 relative to entry 2023-01-05 is -2
    assert any(ln.startswith("2023-01-03") and " | - | - | -2" in ln for ln in lines)
    # Entry day shows percentage stop (not '-') and n_from_entry=0
    entry_line = next(ln for ln in lines if ln.startswith("2023-01-05"))
    parts = [p.strip() for p in entry_line.split("|")]
    # Columns: date, adj_close, ATR, Chandelier, Stop, ΔStop, n
    assert parts[-1] == "0"
    assert parts[-2] == "-"  # ΔStop is '-'
    assert parts[-3] != "-"  # Stop value should be numeric
    # There should be 5 rows after entry (n=1..5)
    post_rows = [ln for ln in lines if ln.startswith("2023-") and ln.endswith(" | 1") or ln.endswith(" | 2") or ln.endswith(" | 3") or ln.endswith(" | 4") or ln.endswith(" | 5")]
    assert len(post_rows) >= 5
