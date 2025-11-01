from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import pytz
import typer

from .config import AppSettings, settings
from .datasrc.base import AbstractDataSource, AdjustedDailyBar
from .engine.broker_ib import DryRunBroker, IBBroker, OrderRequest
from .engine.journal import Journal, Order, Position, Stop
from .engine.notifier import TelegramNotifier
from .rules.chandelier import ChandelierConfig, NotEnoughDataError, trailing_stop

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

    def data_source(self) -> AbstractDataSource:
        source = (self.settings.data.source or "yfinance").lower()
        if source == "yfinance":
            from .datasrc.yfinance import YFinanceDataSource

            return YFinanceDataSource()
        if source == "alphavantage":
            from .datasrc.av import AlphaVantageDataSource

            if not self.settings.data.api_key:
                raise RuntimeError("Alpha Vantage data source requires an API key")
            return AlphaVantageDataSource(api_key=self.settings.data.api_key)
        raise ValueError(f"Unknown data source: {source}")

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


def _market_date(settings: AppSettings) -> date:
    tz = pytz.timezone(settings.tz_market)
    now = datetime.now(tz)
    return now.date()


def _initial_stop_price(fill_price: float, stop_pct: float) -> float:
    if stop_pct <= 0 or stop_pct >= 1:
        raise ValueError("Initial stop percentage must be between 0 and 1.")
    return fill_price * (1 - stop_pct)


def _to_dataframe(data: pd.DataFrame | AdjustedDailyBar) -> pd.DataFrame:
    if isinstance(data, AdjustedDailyBar):
        return data.to_dataframe()
    return data


def _latest_bars(df: pd.DataFrame | AdjustedDailyBar, count: int) -> pd.DataFrame:
    return _to_dataframe(df).tail(count)


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
    initial_stop_pct: float | None = typer.Option(
        None,
        "--initial-stop-pct",
        help="Initial stop-loss percentage (e.g., 0.05 for 5%)",
    ),
    rth: bool = typer.Option(True, help="Regular trading hours only"),
    paper: bool = typer.Option(True, help="Use paper trading account"),
    dry_run: bool = typer.Option(False, help="Dry run mode"),
    sl_type: str = typer.Option("chandelier", "--sl-type", help="Stop-loss method"),
    currency: str = typer.Option("USD", "--currency", help="Order currency"),
) -> None:
    container = build_container()
    journal = container.journal

    existing_position = journal.get_position(symbol)
    if existing_position:
        typer.echo(f"Position for {symbol} already exists with qty={existing_position.qty}")
        raise typer.Exit(code=1)

    data_source = container.data_source()
    bars = data_source.get_adjusted_daily(symbol)
    df = _to_dataframe(bars)
    config = ChandelierConfig(
        atr_period=atr_n or container.settings.strategy.atr_n,
        atr_multiplier=atr_k or container.settings.strategy.atr_k,
        drawdown_pct=container.settings.strategy.drawdown_pct,
    )

    previous_stop = None
    stop_record = journal.get_stop(symbol)
    if stop_record:
        previous_stop = stop_record.stop_price

    if sl_type.lower() != "chandelier":
        raise typer.BadParameter("Only chandelier stop-loss is currently supported")

    if mkt and limit is not None:
        raise typer.BadParameter("Market orders cannot specify a limit price")

    order_type = "MKT" if mkt else "LMT"
    limit_price = None if mkt else limit
    if order_type == "LMT" and limit_price is None:
        raise typer.BadParameter("Limit price must be provided for limit orders")

    latest_bar = df.iloc[-1]
    fill_price = limit_price if limit_price is not None else float(latest_bar["adj_close"])

    stop_pct = initial_stop_pct if initial_stop_pct is not None else container.settings.strategy.initial_stop_pct
    try:
        stop_price = _initial_stop_price(fill_price, stop_pct)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    df_for_calc = _latest_bars(df, config.atr_period * 2)
    try:
        trailing_stop(df_for_calc, config, previous_stop=stop_price)
    except NotEnoughDataError as exc:
        raise typer.BadParameter(str(exc)) from exc

    broker = container.broker(paper=paper, dry_run=dry_run)

    buy_request = OrderRequest(
        symbol=symbol,
        qty=qty,
        side="BUY",
        order_type=order_type,
        limit_price=limit_price,
        tif=tif,
        outside_rth=not rth,
        currency=currency,
    )
    stop_request = OrderRequest(
        symbol=symbol,
        qty=qty,
        side="SELL",
        order_type="STP",
        stop_price=stop_price,
        tif="GTC",
        outside_rth=not rth,
        currency=currency,
    )

    if dry_run:
        typer.echo("Dry run mode: orders will not be sent to IBKR")
    buy_response = broker.place_order(buy_request)
    stop_response = broker.place_order(stop_request)

    typer.echo(f"Buy order status: {buy_response.status}")
    typer.echo(f"Stop order status: {stop_response.status} @ {stop_price:.2f}")

    now = datetime.utcnow()

    journal.upsert_position(
        Position(symbol=symbol, qty=qty, avg_price=fill_price, opened_at=now, paper=paper)
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
    currency: str = typer.Option("USD", "--currency", help="Order currency"),
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
    if mkt and limit is not None:
        raise typer.BadParameter("Market orders cannot specify a limit price")
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
        currency=currency,
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


def _parse_days(days: int) -> int:
    if days <= 0:
        raise typer.BadParameter("Days must be a positive integer.")
    if days > 90:
        raise typer.BadParameter("Maximum allowed window is 90 days.")
    return days


@app.command("sync-data")
def sync_data(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    days: int = typer.Option(30, help="Number of recent days to fetch (max 90)"),
    truncate: bool = typer.Option(False, help="Clear existing cached data for symbol before syncing"),
) -> None:
    days = _parse_days(days)
    container = build_container()
    journal = container.journal

    if truncate:
        journal.clear_price_cache(symbol)

    data_source = container.data_source()
    end_date = _market_date(container.settings)
    start_date = end_date - timedelta(days=days - 1)

    bars = data_source.get_adjusted_daily(symbol)
    df = _to_dataframe(bars).copy()
    df.index = pd.to_datetime(df.index)
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    df = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
    if df.empty:
        typer.echo(f"No data returned for {symbol} in the last {days} days.")
        return

    for price_date, row in df.iterrows():
        journal.cache_price_data(
            symbol=symbol,
            price_date=price_date.date() if isinstance(price_date, pd.Timestamp) else price_date,
            open_price=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            adj_close=float(row["adj_close"]),
        )

    journal.upsert_symbol(symbol)
    typer.echo(f"Cached {len(df)} rows for {symbol} into price_cache.")


@app.command("show-data")
def show_data(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    limit: int = typer.Option(10, help="Number of rows to display"),
    start: Optional[datetime] = typer.Option(None, help="Start date (YYYY-MM-DD)"),
    end: Optional[datetime] = typer.Option(None, help="End date (YYYY-MM-DD)"),
    ascending: bool = typer.Option(False, help="Display in chronological order"),
) -> None:
    if limit is not None and limit <= 0:
        raise typer.BadParameter("Limit must be positive.")

    container = build_container()
    journal = container.journal

    start_date = start.date() if start else None
    end_date = end.date() if end else None

    rows = journal.list_cached_prices(symbol, limit=limit, start=start_date, end=end_date, ascending=ascending)
    if not rows:
        typer.echo(f"No cached data for {symbol}. Run 'strictmode sync-data {symbol}' first.")
        raise typer.Exit(code=0)

    for row in rows:
        typer.echo(
            f"{row['date']} | open={row['open']:.2f} high={row['high']:.2f} "
            f"low={row['low']:.2f} close={row['close']:.2f} adj_close={row['adj_close']:.2f}"
        )


if __name__ == "__main__":
    app()
