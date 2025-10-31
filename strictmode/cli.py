from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

import pandas as pd
import typer

from .config import AppSettings, settings
from .datasrc.av import AlphaVantageDataSource
from .engine.broker_ib import DryRunBroker, IBBroker, OrderRequest
from .engine.journal import Journal, Order, Position, Stop
from .engine.notifier import TelegramNotifier
from .rules.chandelier import ChandelierConfig, trailing_stop

app = typer.Typer(help="StrictMode trading discipline CLI")


class DependencyContainer:
    def __init__(self, app_settings: AppSettings) -> None:
        self.settings = app_settings
        self.journal = Journal(app_settings.database_url)
        self.notifier: TelegramNotifier | None = None
        if app_settings.telegram:
            self.notifier = TelegramNotifier(
                bot_token=app_settings.telegram.bot_token,
                chat_id=app_settings.telegram.chat_id,
            )

    def data_source(self) -> AlphaVantageDataSource:
        return AlphaVantageDataSource(api_key=self.settings.data.api_key)

    def broker(self, paper: bool, dry_run: bool) -> DryRunBroker | IBBroker:
        if dry_run:
            return DryRunBroker()
        return IBBroker(
            host=self.settings.ib.host,
            port=self.settings.ib.port,
            client_id=self.settings.ib.client_id,
            paper=paper,
        )


def _send_notification(container: DependencyContainer, message: str) -> None:
    notifier = container.notifier
    if notifier:
        notifier.send_message(message)


def _compute_initial_stop(
    df: pd.DataFrame,
    config: ChandelierConfig,
    previous_stop: float | None = None,
) -> float:
    stops = trailing_stop(df, config, previous_stop)
    latest_stop = stops.dropna().iloc[-1]
    return float(latest_stop)


def _latest_bars(df: pd.DataFrame, count: int) -> pd.DataFrame:
    return df.tail(count)


def build_container() -> DependencyContainer:
    return DependencyContainer(settings)


@app.command()
def buy(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    qty: float = typer.Argument(..., help="Quantity to buy"),
    limit: Optional[float] = typer.Option(None, "--limit", help="Limit price"),
    mkt: bool = typer.Option(False, "--mkt", help="Use market order"),
    tif: str = typer.Option("GTC", help="Time in force"),
    atr_n: int = typer.Option(None, help="ATR window override"),
    atr_k: float = typer.Option(None, help="ATR multiplier override"),
    rth: bool = typer.Option(True, help="Regular trading hours only"),
    paper: bool = typer.Option(True, help="Use paper trading account"),
    dry_run: bool = typer.Option(False, help="Dry run mode"),
) -> None:
    container = build_container()
    journal = container.journal

    existing_position = journal.get_position(symbol)
    if existing_position:
        typer.echo(f"Position for {symbol} already exists with qty={existing_position.qty}")
        raise typer.Exit(code=1)

    data_source = container.data_source()
    df = data_source.get_adjusted_daily(symbol)
    config = ChandelierConfig(
        atr_period=atr_n or container.settings.strategy.atr_n,
        atr_multiplier=atr_k or container.settings.strategy.atr_k,
        drawdown_pct=container.settings.strategy.drawdown_pct,
    )

    previous_stop = None
    stop_record = journal.get_stop(symbol)
    if stop_record:
        previous_stop = stop_record.stop_price

    df_for_calc = _latest_bars(df, config.atr_period * 2)
    stop_price = _compute_initial_stop(df_for_calc, config, previous_stop)

    order_type = "MKT" if mkt else "LMT"
    limit_price = None if mkt else limit
    if order_type == "LMT" and limit_price is None:
        raise typer.BadParameter("Limit price must be provided for limit orders")

    broker = container.broker(paper=paper, dry_run=dry_run)

    buy_request = OrderRequest(
        symbol=symbol,
        qty=qty,
        side="BUY",
        order_type=order_type,
        limit_price=limit_price,
        tif=tif,
        outside_rth=not rth,
    )
    stop_request = OrderRequest(
        symbol=symbol,
        qty=qty,
        side="SELL",
        order_type="STP",
        stop_price=stop_price,
        tif="GTC",
        outside_rth=not rth,
    )

    if dry_run:
        typer.echo("Dry run mode: orders will not be sent to IBKR")
    buy_response = broker.place_order(buy_request)
    stop_response = broker.place_order(stop_request)

    typer.echo(f"Buy order status: {buy_response.status}")
    typer.echo(f"Stop order status: {stop_response.status} @ {stop_price:.2f}")

    now = datetime.utcnow()

    journal.upsert_position(
        Position(symbol=symbol, qty=qty, avg_price=limit_price or stop_price, opened_at=now, paper=paper)
    )
    journal.upsert_symbol(symbol)
    journal.upsert_stop(
        Stop(
            symbol=symbol,
            stop_price=stop_price,
            method="chandelier",
            atr_n=config.atr_period,
            atr_k=config.atr_multiplier,
            updated_at=now,
        )
    )
    journal.log("INFO", f"Placed buy for {symbol}", ctx=json.dumps({"qty": qty, "stop": stop_price}))

    journal.record_order(
        Order(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side="BUY",
            qty=qty,
            type=order_type,
            limit_price=limit_price,
            stop_price=None,
            tif=tif,
            status=buy_response.status,
            placed_at=now,
        )
    )
    journal.record_order(
        Order(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side="SELL",
            qty=qty,
            type="STP",
            limit_price=None,
            stop_price=stop_price,
            tif="GTC",
            status=stop_response.status,
            placed_at=now,
        )
    )

    _send_notification(
        container,
        f"Buy {symbol} qty={qty} stop={stop_price:.2f} status={buy_response.status}/{stop_response.status}",
    )


@app.command(name="sell-all")
def sell_all(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    qty: float | None = typer.Option(None, help="Quantity override"),
    limit: Optional[float] = typer.Option(None, "--limit", help="Limit price"),
    mkt: bool = typer.Option(False, "--mkt", help="Use market order"),
    tif: str = typer.Option("DAY", help="Time in force"),
    paper: bool = typer.Option(True, help="Use paper trading"),
    dry_run: bool = typer.Option(False, help="Dry run mode"),
) -> None:
    container = build_container()
    journal = container.journal

    position = journal.get_position(symbol)
    if position is None:
        typer.echo(f"No open position for {symbol}")
        raise typer.Exit(code=1)

    order_qty = qty or position.qty
    order_type = "MKT" if mkt else "LMT"
    limit_price = None if mkt else limit
    if order_type == "LMT" and limit_price is None:
        raise typer.BadParameter("Limit price must be provided for limit orders")

    broker = container.broker(paper=paper, dry_run=dry_run)

    stop = journal.get_stop(symbol)
    if stop:
        # 取消IBKR上的实际止损单
        try:
            stop_orders = broker.find_stop_orders(symbol)
            for order_id, _ in stop_orders:
                if not dry_run:
                    broker.cancel_order(order_id)
                    typer.echo(f"Cancelled stop order {order_id} for {symbol}")
                else:
                    typer.echo(f"[DRY RUN] Would cancel stop order {order_id} for {symbol}")
        except Exception as e:
            typer.echo(f"Warning: Failed to cancel stop orders: {e}", err=True)
        journal.delete_stop(symbol)

    sell_request = OrderRequest(
        symbol=symbol,
        qty=order_qty,
        side="SELL",
        order_type=order_type,
        limit_price=limit_price,
        tif=tif,
    )

    if dry_run:
        typer.echo("Dry run mode: order will not be sent to IBKR")
    sell_response = broker.place_order(sell_request)
    typer.echo(f"Sell order status: {sell_response.status}")

    journal.delete_position(symbol)
    journal.log("INFO", f"Closed position for {symbol}")

    now = datetime.utcnow()
    journal.record_order(
        Order(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side="SELL",
            qty=order_qty,
            type=order_type,
            limit_price=limit_price,
            stop_price=None,
            tif=tif,
            status=sell_response.status,
            placed_at=now,
        )
    )

    _send_notification(container, f"Sell {symbol} qty={order_qty} status={sell_response.status}")


if __name__ == "__main__":
    app()
