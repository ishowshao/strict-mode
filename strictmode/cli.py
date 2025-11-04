from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
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
from .engine.analysis import build_chandelier_table
from .engine.ticks import hk_tick, round_to_increment

app = typer.Typer(
    help=(
        "StrictMode trading discipline CLI\n\n"
        "市场与代码格式: 仅支持美股与港股。港股代码必须以 .HK 结尾，且应为 4 位数字加 .HK（如 0700.HK、9988.HK）。"
        "未带 .HK 的代码按美股处理；若为纯数字且查询无数据，CLI 会给出友好提示。\n"
        "币种默认值: 美股默认 USD；.HK 结尾的港股默认 HKD（可用 --currency 显式覆盖）。"
    )
)


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


def _hk_hint(symbol: str) -> str | None:
    """Return a friendly hint for HK symbols based on input format.

    Policy: only US and HK supported. HK must end with .HK.
    """
    sym = str(symbol).strip()
    sym_u = sym.upper()
    if not sym_u.endswith(".HK") and sym.isdigit():
        padded = sym.zfill(4)
        return (
            f"检测到纯数字代码。如为港股，请使用 {padded}.HK 形式（示例：9988.HK）。"
            "当前仅支持美股与港股：未带 .HK 的代码将按美股处理。"
        )
    if sym_u.endswith(".HK") and not sym[:-3].isdigit():
        return "港股代码需为4位数字加 .HK，例如 0700.HK、09888.HK。"
    return None


def _ib_symbol_matches(contract_symbol: str | None, user_symbol: str) -> bool:
    if not contract_symbol:
        return False
    sym_u = user_symbol.upper().strip()
    if sym_u.endswith(".HK"):
        core = "".join(ch for ch in sym_u[:-3] if ch.isdigit())
        ib_sym = core.lstrip("0") or "0"
        return contract_symbol == ib_sym
    return contract_symbol == user_symbol


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
    currency: Optional[str] = typer.Option(None, "--currency", help="Order currency (default: USD; HK tickers default to HKD)"),
    ib_debug: bool = typer.Option(False, "--ib-debug", help="Print IB API debug events"),
) -> None:
    container = build_container()
    journal = container.journal

    data_source = container.data_source()
    # Fetch price bars with friendly error handling for HK tickers
    try:
        bars = data_source.get_adjusted_daily(symbol)
    except Exception as e:
        hint = _hk_hint(symbol)
        typer.echo(f"获取行情失败：{e}")
        if hint:
            typer.echo(f"提示：{hint}")
        raise typer.Exit(code=1)
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
    fill_price = limit_price if limit_price is not None else float(latest_bar["close"])

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
    if not dry_run and isinstance(broker, IBBroker) and ib_debug:
        broker.enable_debug(True)

    # Determine order currency: HK tickers default to HKD; otherwise USD.
    order_currency = currency or ("HKD" if str(symbol).upper().endswith(".HK") else "USD")

    order_ref = f"SM:{symbol}"
    buy_request = OrderRequest(
        symbol=symbol,
        qty=qty,
        side="BUY",
        order_type=order_type,
        limit_price=limit_price,
        tif=tif,
        outside_rth=not rth,
        currency=order_currency,
        # Transmit parent immediately so TWS shows it as accepted
        transmit=None,  # default True in broker
        order_ref=order_ref,
    )
    stop_request = OrderRequest(
        symbol=symbol,
        qty=qty,
        side="SELL",
        order_type="STP",
        stop_price=stop_price,
        tif="GTC",
        outside_rth=not rth,
        currency=order_currency,
        order_ref=order_ref,
    )

    if dry_run:
        typer.echo("Dry run mode: orders will not be sent to IBKR")
    if not dry_run:
        # Use bracket placement so child is attached and visible in TWS
        buy_response, stop_response = broker.place_bracket(buy_request, stop_request)  # type: ignore[attr-defined]
    else:
        buy_response = broker.place_order(buy_request)
        stop_response = broker.place_order(stop_request)

    typer.echo(f"Buy order status: {buy_response.status}")
    typer.echo(f"Stop order status: {stop_response.status} @ {stop_price:.2f}")
    if buy_response.order_id is not None and stop_response.order_id is not None:
        typer.echo(f"IB IDs: parent={buy_response.order_id} -> child={stop_response.order_id}")

    now = datetime.now(timezone.utc)

    # Position aggregation: allow multiple BUYs; maintain weighted avg
    existing_position = journal.get_position(symbol)
    if existing_position:
        new_qty = existing_position.qty + qty
        new_avg = (
            existing_position.avg_price * existing_position.qty + fill_price * qty
        ) / new_qty
        journal.upsert_position(
            Position(
                symbol=symbol,
                qty=new_qty,
                avg_price=new_avg,
                opened_at=existing_position.opened_at,
                paper=existing_position.paper,
            )
        )
    else:
        journal.upsert_position(
            Position(symbol=symbol, qty=qty, avg_price=fill_price, opened_at=now, paper=paper)
        )
    journal.upsert_symbol(symbol)
    # DB keeps per-symbol analysis floor (Chandelier). Do not lower existing record
    existing_stop = journal.get_stop(symbol)
    db_stop_price = max(stop_price, existing_stop.stop_price) if existing_stop else stop_price
    journal.upsert_stop(
        Stop(
            symbol=symbol,
            stop_price=db_stop_price,
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
    currency: Optional[str] = typer.Option(
        None,
        "--currency",
        help="Order currency (default: USD; HK tickers default to HKD)",
    ),
) -> None:
    container = build_container()
    journal = container.journal

    position = journal.get_position(symbol)
    if position is None:
        typer.echo(f"No open position for {symbol}")
        hint = _hk_hint(symbol)
        if hint:
            typer.echo(f"提示：{hint}")
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

    # Determine order currency: HK tickers default to HKD; otherwise USD.
    order_currency = currency or ("HKD" if str(symbol).upper().endswith(".HK") else "USD")

    sell_request = OrderRequest(
        symbol=symbol,
        qty=order_qty,
        side="SELL",
        order_type=order_type,
        limit_price=limit_price,
        tif=tif,
        currency=order_currency,
    )

    if dry_run:
        typer.echo("Dry run mode: order will not be sent to IBKR")
    sell_response = broker.place_order(sell_request)
    typer.echo(f"Sell order status: {sell_response.status}")

    journal.delete_position(symbol)
    journal.log("INFO", f"Closed position for {symbol}")

    now = datetime.now(timezone.utc)
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

    # Fetch price bars with friendly error handling for HK tickers
    try:
        bars = data_source.get_adjusted_daily(symbol)
    except Exception as e:
        hint = _hk_hint(symbol)
        typer.echo(f"获取行情失败：{e}")
        if hint:
            typer.echo(f"提示：{hint}")
        raise typer.Exit(code=1)
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
        hint = _hk_hint(symbol)
        if hint:
            typer.echo(f"提示：{hint}")
        raise typer.Exit(code=0)

    for row in rows:
        typer.echo(
            f"{row['date']} | open={row['open']:.2f} high={row['high']:.2f} "
            f"low={row['low']:.2f} close={row['close']:.2f} adj_close={row['adj_close']:.2f}"
        )

@app.command("chandelier-table")
def chandelier_table(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    entry: Optional[datetime] = typer.Option(
        None, "--entry", help="Entry date (YYYY-MM-DD). Default: first calculable day"
    ),
    days: Optional[int] = typer.Option(
        None, "--days", help="Number of trading days after entry to display"
    ),
    atr_n: Optional[int] = typer.Option(None, help="ATR window override"),
    atr_k: Optional[float] = typer.Option(None, help="ATR multiplier override"),
    ascending: bool = typer.Option(True, help="Print in chronological order"),
    csv: Optional[str] = typer.Option(None, "--csv", help="Optional CSV output path"),
) -> None:
    """Print a verification table of ATR/Chandelier and trailing stop using cached data only.

    - Uses canonical ATR/Chandelier functions from rules.chandelier
    - Trailing stop starts from the chosen entry day and never decreases
    - Shows rows strictly after entry day: n_from_entry = 1..N
    """
    if days is not None and days <= 0:
        raise typer.BadParameter("--days must be positive")

    container = build_container()
    journal = container.journal

    # Pull all cached rows in ascending order
    rows = journal.list_cached_prices(symbol, limit=None, start=None, end=None, ascending=True)
    if not rows:
        typer.echo(f"No cached data for {symbol}. Run 'strictmode sync-data {symbol}' first.")
        hint = _hk_hint(symbol)
        if hint:
            typer.echo(f"提示：{hint}")
        raise typer.Exit(code=1)

    config = ChandelierConfig(
        atr_period=atr_n or container.settings.strategy.atr_n,
        atr_multiplier=atr_k or container.settings.strategy.atr_k,
        drawdown_pct=None,  # explicitly off for verification
    )

    try:
        result = build_chandelier_table(
            symbol,
            rows,
            config=config,
            entry=entry.date() if entry else None,
            days=days,
            initial_stop_pct=container.settings.strategy.initial_stop_pct,
        )
    except NotEnoughDataError as e:
        typer.echo(f"数据不足：{e}")
        raise typer.Exit(code=1)
    except Exception as e:  # noqa: BLE001
        typer.echo(f"生成表格失败：{e}")
        raise typer.Exit(code=1)

    df = result.table.copy()
    if df.empty:
        typer.echo("No rows to display for the requested window.")
        raise typer.Exit(code=0)

    # Formatting
    df_print = df.copy()
    # numeric formatting
    for col in ("adj_close", "atr", "chandelier"):
        if col in df_print.columns:
            df_print[col] = df_print[col].astype(float).map(lambda x: f"{x:.4f}" if pd.notna(x) else "")

    used_pct = float(container.settings.strategy.initial_stop_pct)
    header = (
        f"{symbol} | ATR(n={result.config.atr_period}) k={result.config.atr_multiplier} "
        f"init_stop={used_pct*100:.1f}% | entry={result.entry_date.date()}"
    )
    typer.echo(header)
    # Print header row
    typer.echo("date | adj_close | ATR | Chandelier | Stop(trailing) | ΔStop | n_from_entry")
    iter_df = df_print if ascending else df_print.iloc[::-1]
    for idx, row in iter_df.iterrows():
        n = int(row["n_from_entry"])  # type: ignore[index]
        # stop/delta formatting: show '-' for n<0; entry day delta '-'
        stop_val = df.loc[idx, "stop_trailing"]
        if n < 0 or pd.isna(stop_val):
            stop_str = "-"
            delta_str = "-"
        else:
            stop_str = f"{float(stop_val):.4f}"
            delta_val = df.loc[idx, "delta_stop"]
            delta_str = f"{float(delta_val):.4f}" if pd.notna(delta_val) and n >= 1 else "-"

        typer.echo(
            f"{idx.date()} | {row['adj_close']} | {row['atr']} | {row['chandelier']} | "
            f"{stop_str} | {delta_str} | {n}"
        )

    if csv:
        try:
            # Save unformatted numeric values
            df.to_csv(csv, index=True, index_label="date")
            typer.echo(f"Saved CSV to {csv}")
        except Exception as e:  # noqa: BLE001
            typer.echo(f"保存 CSV 失败：{e}", err=True)
            
if __name__ == "__main__":
    app()
@app.command("show-orders")
def show_orders(
    paper: bool = typer.Option(True, help="Use paper trading account"),
    state: str = typer.Option(
        "live",
        "--state",
        case_sensitive=False,
        help="Match TWS filters: all|live|cancelled|completed (default: live)",
    ),
    api_only: bool = typer.Option(True, help="Completed orders: API-only scope (IB API setting)"),
) -> None:
    """Show IBKR orders using TWS-style filters.

    - live: active/working orders (PendingSubmit/PreSubmitted/Submitted/PendingCancel)
    - cancelled: Cancelled/ApiCancelled (from Completed API)
    - completed: Filled/Inactive (non-cancel final states from Completed API)
    - all: live + cancelled + completed
    """
    container = build_container()
    broker = container.broker(paper=paper, dry_run=False)

    rows: list[dict] = []
    state_key = state.strip().lower()
    allowed = {"all", "live", "cancelled", "completed"}
    if state_key not in allowed:
        raise typer.BadParameter(f"Invalid state '{state}'. Choose from: all, live, cancelled, completed")
    try:
        if state_key in ("live", "all"):
            list_open = getattr(broker, "list_open_orders", None)
            if callable(list_open):
                rows.extend(list_open())
        if state_key in ("completed", "cancelled", "all"):
            list_completed = getattr(broker, "list_completed_orders", None)
            completed = list_completed(api_only=api_only) if callable(list_completed) else []
            upper = [dict(r) for r in completed]
            # Split completed into cancelled vs completed per TWS semantics
            cancelled_set = {"CANCELLED", "APICANCELLED"}
            completed_set = {"FILLED", "INACTIVE", "EXPIRED"}
            if state_key == "cancelled":
                rows.extend([r for r in upper if str(r.get("status", "")).upper() in cancelled_set])
            elif state_key == "completed":
                rows.extend([r for r in upper if str(r.get("status", "")).upper() in completed_set])
            else:  # all -> include both
                rows.extend(upper)
    except Exception as e:  # pragma: no cover - runtime only
        typer.echo(f"Failed to fetch orders: {e}", err=True)
        raise typer.Exit(code=1)

    if not rows:
        typer.echo("No orders matching filters.")
        return

    for r in rows:
        typer.echo(
            f"id={r.get('orderId')} parent={r.get('parentId')} sym={r.get('symbol')} {r.get('action')} "
            f"{r.get('type')} tif={r.get('tif')} lmt={r.get('lmtPrice')} stp={r.get('auxPrice')} status={r.get('status')}"
        )


@app.command("tick-size")
def tick_size(
    symbol: str = typer.Argument(..., help="Ticker symbol (.HK for Hong Kong)"),
    price: float = typer.Argument(..., help="Reference price to evaluate increment"),
    mode: str = typer.Option("nearest", help="Rounding mode: nearest|down|up"),
) -> None:
    """Offline helper to show tick size and rounded price without connecting to IBKR.

    - For .HK symbols, uses HKEX main board table.
    - Otherwise, assumes 0.01 increment (US stocks).
    """
    sym_u = symbol.upper().strip()
    if sym_u.endswith(".HK"):
        inc = hk_tick(price)
        exch = "SEHK"
    else:
        inc = 0.01
        exch = "US/SMART"
    rounded = round_to_increment(price, inc, mode=mode)
    typer.echo(f"exchange={exch} inc={inc} price={price} -> rounded={rounded} (mode={mode})")


@app.command("reconcile-stops")
def reconcile_stops(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    apply: bool = typer.Option(False, "--apply", help="Apply cancellations to reconcile stops"),
    paper: bool = typer.Option(True, help="Use paper trading account"),
    dry_run: bool = typer.Option(False, help="Dry run mode for planning only"),
) -> None:
    """Compare per-symbol STOP quantities with current position and plan cancellations to avoid oversell."""
    container = build_container()
    journal = container.journal
    position = journal.get_position(symbol)
    pos_qty = position.qty if position else 0.0

    broker = container.broker(paper=paper, dry_run=dry_run)
    if isinstance(broker, DryRunBroker):
        typer.echo("DryRun broker: inferring open orders from dry-run stash")
    try:
        rows = broker.list_open_orders()  # type: ignore[attr-defined]
    except Exception as e:  # pragma: no cover - runtime only
        typer.echo(f"Failed to fetch open orders: {e}", err=True)
        raise typer.Exit(code=1)

    # Filter STOPs belonging to StrictMode for this symbol
    stops = [
        r
        for r in rows
        if _ib_symbol_matches(r.get("symbol"), symbol)
        and str(r.get("type", "")).upper().startswith("STP")
        and isinstance(r.get("orderRef"), str)
        and r.get("orderRef", "").startswith("SM:")
    ]
    total_stop_qty = float(sum(float(r.get("totalQuantity") or 0.0) for r in stops))
    typer.echo(
        f"Position qty={pos_qty}, STOPs count={len(stops)}, total STOP qty={total_stop_qty}"
    )

    if total_stop_qty <= pos_qty:
        typer.echo("No reconciliation needed: STOP qty <= position qty")
        return

    # Plan: cancel whole STOP orders until total <= pos
    # Prefer to cancel orders with highest stop price (closest to current price)
    excess = total_stop_qty - pos_qty
    stops_sorted = sorted(
        stops,
        key=lambda r: (float(r.get("auxPrice") or r.get("lmtPrice") or 0.0), int(r.get("orderId") or 0)),
        reverse=True,
    )
    plan_ids: list[int] = []
    remaining = excess
    for r in stops_sorted:
        if remaining <= 1e-9:
            break
        q = float(r.get("totalQuantity") or 0.0)
        plan_ids.append(int(r.get("orderId")))
        remaining -= q

    typer.echo(f"Excess STOP qty={excess}. Plan to cancel {len(plan_ids)} order(s): {plan_ids}")
    if not apply or dry_run:
        typer.echo("Dry-run/preview only. Use --apply to execute.")
        return

    # Execute
    cancelled = 0
    for oid in plan_ids:
        try:
            broker.cancel_order(oid)
            cancelled += 1
            typer.echo(f"Cancelled STOP order {oid}")
        except Exception as e:  # pragma: no cover - runtime only
            typer.echo(f"Failed to cancel {oid}: {e}", err=True)
    typer.echo(f"Reconcile complete. Cancelled={cancelled}")


@app.command("cancel")
def cancel_orders(
    ids: list[int] = typer.Option(None, "--id", help="OrderId to cancel (repeatable)", show_default=False),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="Symbol filter (maps to orderRef=SM:{symbol})"),
    order_ref: Optional[str] = typer.Option(None, "--order-ref", help="OrderRef prefix to filter (e.g., SM:TSLA)"),
    include_children: bool = typer.Option(True, help="Also cancel child orders of selected parents"),
    paper: bool = typer.Option(True, help="Use paper trading account"),
    dry_run: bool = typer.Option(False, help="Preview cancellations without sending to IB"),
    apply: bool = typer.Option(False, "--apply", help="Execute cancellations"),
) -> None:
    """Cancel live (working) orders by id or group. Defaults to include children."""
    container = build_container()
    broker = container.broker(paper=paper, dry_run=dry_run)

    # Gather open (live) orders snapshot
    try:
        rows = broker.list_open_orders()  # type: ignore[attr-defined]
    except Exception as e:  # pragma: no cover - runtime only
        typer.echo(f"Failed to fetch open orders: {e}", err=True)
        raise typer.Exit(code=1)

    # Resolve filter by orderRef
    ref_prefix = order_ref
    if ref_prefix is None and symbol is not None:
        ref_prefix = f"SM:{symbol}"

    # Select candidates
    selected_ids: set[int] = set()
    if ids:
        id_set = set(int(i) for i in ids)
        for r in rows:
            oid = int(r.get("orderId") or 0)
            if oid in id_set:
                selected_ids.add(oid)
    if ref_prefix:
        for r in rows:
            ref = r.get("orderRef")
            if isinstance(ref, str) and ref.startswith(ref_prefix):
                oid = int(r.get("orderId") or 0)
                selected_ids.add(oid)

    if not selected_ids:
        typer.echo("No matching live orders to cancel.")
        return

    # Optionally include child orders for selected parents
    if include_children:
        parents = set(selected_ids)
        for r in rows:
            pid = r.get("parentId")
            try:
                pid_int = int(pid) if pid is not None else None
            except Exception:
                pid_int = None
            if pid_int and pid_int in parents:
                selected_ids.add(int(r.get("orderId") or 0))

    plan = sorted(selected_ids)
    typer.echo(f"Plan to cancel {len(plan)} order(s): {plan}")
    if dry_run or not apply:
        typer.echo("Dry-run/preview only. Use --apply to execute.")
        return

    # Execute cancellations
    ok = 0
    for oid in plan:
        try:
            broker.cancel_order(oid)
            ok += 1
            typer.echo(f"Cancelled order {oid}")
        except Exception as e:  # pragma: no cover - runtime only
            typer.echo(f"Failed to cancel {oid}: {e}", err=True)
    typer.echo(f"Done. Cancelled={ok} / {len(plan)}")
