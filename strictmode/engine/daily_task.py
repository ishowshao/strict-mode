from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pandas as pd
import pytz

from ..cli import DependencyContainer
from .broker_ib import OrderRequest
from .journal import Stop
from ..rules.chandelier import ChandelierConfig, NotEnoughDataError, trailing_stop


def _get_market_date(settings) -> date:
    """获取市场时区的当前日期"""
    tz = pytz.timezone(settings.tz_market)
    now = datetime.now(tz)
    return now.date()


def _check_data_freshness(latest_date: date, target_date: date, symbol: str, container: DependencyContainer) -> bool:
    """检查数据新鲜度，如果滞后则发送告警并返回False"""
    if latest_date < target_date:
        msg = f"Data lag alert for {symbol}: latest={latest_date}, target={target_date}"
        container.journal.log("WARNING", msg)
        if container.notifier:
            container.notifier.send_message(f"⚠️ {msg}")
        return False
    return True


def daily_update_task(container: DependencyContainer) -> None:
    """每日任务：更新所有持仓的止损价"""
    journal = container.journal
    settings = container.settings
    target_date = _get_market_date(settings)

    # 获取所有持仓
    positions = journal.get_all_positions()
    if not positions:
        container.journal.log("INFO", "No positions to update")
        return

    summary_messages: list[str] = []
    updated_count = 0
    triggered_count = 0
    error_count = 0

    for position in positions:
        symbol = position.symbol
        try:
            # 获取止损配置
            stop_record = journal.get_stop(symbol)
            if not stop_record:
                journal.log("WARNING", f"No stop record found for {symbol}, skipping")
                continue

            config = ChandelierConfig(
                atr_period=stop_record.atr_n,
                atr_multiplier=stop_record.atr_k,
                drawdown_pct=settings.strategy.drawdown_pct,
            )

            # 获取数据
            data_source = container.data_source()
            df = data_source.get_adjusted_daily(symbol)

            # 检查数据新鲜度
            latest_date = df.index[-1].date() if isinstance(df.index[-1], pd.Timestamp) else df.index[-1]
            if not _check_data_freshness(latest_date, target_date, symbol, container):
                error_count += 1
                continue

            # 检查数据量是否足够
            if len(df) < config.atr_period:
                msg = f"Insufficient data for {symbol}: {len(df)} < {config.atr_period}"
                journal.log("WARNING", msg)
                if container.notifier:
                    container.notifier.send_message(f"⚠️ {msg}")
                error_count += 1
                continue

            # 计算最新止损价
            df_for_calc = df.tail(config.atr_period * 2)
            previous_stop = stop_record.stop_price
            stops = trailing_stop(df_for_calc, config, previous_stop)
            stop_today = float(stops.dropna().iloc[-1])

            # 获取最新收盘价
            latest_bar = df.iloc[-1]
            adj_close_today = float(latest_bar["adj_close"])

            # 缓存最新数据
            journal.cache_price_data(
                symbol=symbol,
                price_date=latest_date,
                open_price=float(latest_bar["open"]),
                high=float(latest_bar["high"]),
                low=float(latest_bar["low"]),
                close=float(latest_bar["close"]),
                adj_close=adj_close_today,
            )

            # 检查是否触发止损
            if adj_close_today <= stop_today:
                triggered_count += 1
                msg = f"🛑 Stop triggered for {symbol}: close={adj_close_today:.2f} <= stop={stop_today:.2f}"
                journal.log("WARNING", msg)
                summary_messages.append(msg)

                if settings.strategy.auto_liquidate and not position.paper:
                    # 自动清仓
                    broker = container.broker(paper=position.paper, dry_run=False)
                    try:
                        # 取消止损单
                        stop_orders = broker.find_stop_orders(symbol)
                        for order_id, _ in stop_orders:
                            broker.cancel_order(order_id)
                        # 下达市价平仓单
                        sell_request = broker.place_order(
                            OrderRequest(
                                symbol=symbol,
                                qty=position.qty,
                                side="SELL",
                                order_type="MKT",
                                tif="DAY",
                            )
                        )
                        journal.delete_position(symbol)
                        journal.delete_stop(symbol)
                        msg += f" (Auto-liquidated: {sell_request.status})"
                    except Exception as e:
                        msg += f" (Auto-liquidation failed: {e})"
                        journal.log("ERROR", f"Auto-liquidation failed for {symbol}: {e}")
                else:
                    # 仅通知
                    if container.notifier:
                        container.notifier.send_message(f"🚨 {msg}")

            # 检查是否需要更新止损单（逐张上调到不低于Chandelier，不下调）
            elif stop_today > previous_stop:
                broker = container.broker(paper=position.paper, dry_run=False)
                try:
                    updated_this_symbol = 0
                    deltas: list[float] = []
                    stop_orders = broker.find_stop_orders(symbol, order_ref_prefix="SM:")
                    if stop_orders:
                        for order_id, current in stop_orders:
                            new_price = max(float(current), float(stop_today))
                            if new_price > float(current) + 1e-9:
                                broker.modify_order(order_id, stop_price=new_price)
                                updated_this_symbol += 1
                                deltas.append(new_price - float(current))
                        if updated_this_symbol:
                            updated_count += updated_this_symbol
                            msg = (
                                f"📈 {symbol} raised {updated_this_symbol} stop(s) to >= {stop_today:.2f}"
                            )
                            if deltas:
                                msg += f" (minΔ={min(deltas):.2f}, maxΔ={max(deltas):.2f})"
                            journal.log("INFO", msg)
                            summary_messages.append(msg)
                    else:
                        msg = f"⚠️ No stop order found for {symbol} to update"
                        journal.log("WARNING", msg)
                except Exception as e:
                    msg = f"Failed to update stop for {symbol}: {e}"
                    journal.log("ERROR", msg)
                    error_count += 1

                # 更新数据库为当日Chandelier值
                journal.upsert_stop(
                    Stop(
                        symbol=symbol,
                        stop_price=stop_today,
                        method=stop_record.method,
                        atr_n=stop_record.atr_n,
                        atr_k=stop_record.atr_k,
                        updated_at=datetime.now(timezone.utc),
                    )
                )

        except NotEnoughDataError as e:
            msg = f"Insufficient data for {symbol}: {e}"
            journal.log("WARNING", msg)
            error_count += 1
        except Exception as e:
            msg = f"Error processing {symbol}: {e}"
            journal.log("ERROR", msg, ctx=json.dumps({"error": str(e)}))
            error_count += 1

    # 发送日报摘要
    summary = f"📊 Daily Update Summary ({target_date}):\n"
    summary += f"✅ Updated: {updated_count}\n"
    summary += f"🛑 Triggered: {triggered_count}\n"
    summary += f"❌ Errors: {error_count}\n"
    if summary_messages:
        summary += "\n" + "\n".join(summary_messages)

    journal.log("INFO", f"Daily update completed: updated={updated_count}, triggered={triggered_count}, errors={error_count}")
    if container.notifier:
        container.notifier.send_message(summary)
