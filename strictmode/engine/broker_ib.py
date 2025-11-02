from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from math import floor


def _import_ib_components():  # pragma: no cover - thin wrapper for retry
    from ib_insync import IB, Contract, Order

    return IB, Contract, Order


try:
    IB, Contract, Order = _import_ib_components()
except RuntimeError as exc:  # pragma: no cover - handle asyncio policy on Py3.14+
    if "no current event loop" in str(exc).lower():
        import asyncio

        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            IB, Contract, Order = _import_ib_components()
        except Exception:  # pragma: no cover - fallback to dummy types
            IB = None  # type: ignore
            Contract = object  # type: ignore
            Order = object  # type: ignore
    else:  # pragma: no cover - unexpected runtime error
        raise
except Exception:  # pragma: no cover - ib_insync may not be installed in tests
    IB = None  # type: ignore
    Contract = object  # type: ignore
    Order = object  # type: ignore


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    qty: float
    side: str  # BUY or SELL
    order_type: str  # MKT or LMT or STP
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = "GTC"
    outside_rth: bool = False
    currency: str = "USD"
    # IBKR-specific linkage fields for bracket/child orders
    parent_id: int | None = None
    transmit: bool | None = None


@dataclass(slots=True)
class OrderResponse:
    order_id: int | None
    status: str
    description: str


class IBBroker:
    def __init__(self, host: str, port: int, client_id: int, paper: bool = True) -> None:
        if IB is None:
            raise RuntimeError("ib_insync is required for broker operations")
        self.host = host
        self.port = port
        self.client_id = client_id
        self.paper = paper
        self._ib: Optional[IB] = None
        self._debug_enabled = False

    @property
    def ib(self) -> IB:
        if self._ib is None:
            self._ib = IB()
        return self._ib

    def connect(self) -> None:
        if not self.ib.isConnected():  # type: ignore[attr-defined]
            self.ib.connect(self.host, self.port, clientId=self.client_id)
            if self._debug_enabled:
                self._attach_debug_handlers()

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():  # type: ignore[attr-defined]
            self._ib.disconnect()

    def _contract(self, symbol: str, currency: str) -> Contract:
        from ib_insync import Stock
        # Qualify the contract to avoid ambiguous symbols that can cause PendingSubmit
        contract = Stock(symbol, "SMART", currency)
        try:
            self.connect()
            qualified = self.ib.qualifyContracts(contract)  # type: ignore[attr-defined]
            if qualified:
                return qualified[0]
        except Exception:
            # Fallback to unqualified contract; IB may still accept it
            pass
        return contract

    # --- Price increment helpers -------------------------------------------
    def _price_increment(self, contract: Contract, price: float) -> float:
        """Best-effort fetch of min tick increment for a given price.

        Falls back to 0.01 for stocks if market rules unavailable.
        """
        try:
            cds = self.ib.reqContractDetails(contract)  # type: ignore[attr-defined]
            if cds:
                cd = cds[0]
                # Legacy field
                min_tick = getattr(cd, "minTick", None)
                if isinstance(min_tick, (int, float)) and min_tick and min_tick > 0:
                    return float(min_tick)
                # Market rule-based increments
                market_rule_ids = getattr(cd, "marketRuleIds", None)
                if market_rule_ids:
                    rid_str = str(market_rule_ids).split(",")[0].strip()
                    rid = int(rid_str)
                    increments = self.ib.reqMarketRule(rid)  # type: ignore[attr-defined]
                    if increments:
                        inc: float | None = None
                        for pi in increments:
                            # Choose the increment with highest lowEdge <= price
                            if price >= float(getattr(pi, "lowEdge", 0.0)):
                                inc = float(getattr(pi, "increment", 0.01))
                            else:
                                break
                        if inc:
                            return inc
        except Exception:
            pass
        # Default for US stocks
        return 0.01

    def _round_to_increment(self, price: float, inc: float, mode: str = "nearest") -> float:
        if inc <= 0:
            return price
        # Avoid FP artifacts
        steps = price / inc
        if mode == "down":
            steps = floor(steps + 1e-9)
        elif mode == "up":
            steps = floor(steps + 0.999999)
        else:
            steps = round(steps)
        return round(steps * inc, 10)

    # --- Debug helpers -----------------------------------------------------
    def enable_debug(self, enabled: bool = True) -> None:
        self._debug_enabled = enabled
        if enabled and self._ib and self._ib.isConnected():  # type: ignore[attr-defined]
            self._attach_debug_handlers()

    def _attach_debug_handlers(self) -> None:  # pragma: no cover - runtime aid
        try:
            ib = self.ib
            # Guard against double-registration
            if getattr(self, "_debug_attached", False):
                return
            self._debug_attached = True  # type: ignore[attr-defined]

            def on_error(reqId, code, msg, misc):  # noqa: N803 - IBKR casing
                print(f"[IB][ERROR] id={reqId} code={code} msg={msg} misc={misc}")

            def on_orderStatus(trade):  # noqa: N802 - IBKR casing
                os = trade.orderStatus
                print(
                    f"[IB][STATUS] id={trade.order.orderId} status={os.status} filled={os.filled} remaining={os.remaining}"
                )

            ib.errorEvent += on_error
            ib.orderStatusEvent += on_orderStatus
        except Exception:
            pass

    def place_order(self, request: OrderRequest) -> OrderResponse:
        self.connect()
        contract = self._contract(request.symbol, request.currency)
        from ib_insync import Order

        order = Order(
            action="BUY" if request.side.upper() == "BUY" else "SELL",
            orderType=request.order_type.upper(),
            totalQuantity=abs(request.qty),
            tif=("DAY" if request.order_type.upper() == "MKT" and request.tif.upper() == "GTC" else request.tif),
            lmtPrice=request.limit_price,
            auxPrice=request.stop_price,
            outsideRth=request.outside_rth,
            parentId=request.parent_id,
            transmit=True if request.transmit is None else bool(request.transmit),
        )
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)
        status = trade.orderStatus.status
        order_id = trade.order.orderId
        return OrderResponse(order_id=order_id, status=status, description=str(trade.order))

    def place_bracket(self, parent: OrderRequest, stop: OrderRequest) -> tuple[OrderResponse, OrderResponse]:
        """Place a parent order with a stop-loss child as a bracket.

        - Parent is submitted with transmit=False to stage the chain.
        - Stop child references parent via parentId and transmits the chain.
        - Both legs share the same qualified Contract to avoid PendingSubmit.
        """
        self.connect()
        if parent.symbol != stop.symbol or parent.currency != stop.currency:
            raise ValueError("Bracket legs must use same symbol/currency")

        contract = self._contract(parent.symbol, parent.currency)
        from ib_insync import Order

        # Round prices to valid tick increments to avoid error 110
        parent_inc = self._price_increment(contract, float(parent.limit_price or parent.stop_price or 0.0))
        stop_inc = self._price_increment(contract, float(stop.stop_price or stop.limit_price or 0.0))
        if parent.order_type.upper() == "LMT" and parent.limit_price is not None:
            parent.limit_price = self._round_to_increment(float(parent.limit_price), parent_inc, mode="nearest")
        if stop.stop_price is not None:
            # For SELL stop on long, rounding to nearest tick is fine
            stop.stop_price = self._round_to_increment(float(stop.stop_price), stop_inc, mode="nearest")

        parent_order = Order(
            action="BUY" if parent.side.upper() == "BUY" else "SELL",
            orderType=parent.order_type.upper(),
            totalQuantity=abs(parent.qty),
            tif=("DAY" if parent.order_type.upper() == "MKT" and parent.tif.upper() == "GTC" else parent.tif),
            lmtPrice=parent.limit_price,
            auxPrice=parent.stop_price,
            outsideRth=parent.outside_rth,
            transmit=False,
        )
        parent_trade = self.ib.placeOrder(contract, parent_order)
        # Ensure parentId is available for child
        self.ib.sleep(0.5)
        parent_id = parent_trade.order.orderId

        stop_order = Order(
            action="SELL",  # stop-loss is always a sell against the long
            orderType=stop.order_type.upper(),  # "STP"
            totalQuantity=abs(stop.qty),
            tif=stop.tif,
            lmtPrice=stop.limit_price,
            auxPrice=stop.stop_price,
            outsideRth=stop.outside_rth,
            parentId=parent_id,
            transmit=True,
        )
        stop_trade = self.ib.placeOrder(contract, stop_order)
        self.ib.sleep(1)

        return (
            OrderResponse(order_id=parent_id, status=parent_trade.orderStatus.status, description=str(parent_trade.order)),
            OrderResponse(order_id=stop_trade.order.orderId, status=stop_trade.orderStatus.status, description=str(stop_trade.order)),
        )

    def cancel_order(self, order_id: int) -> None:
        self.connect()
        orders = self.ib.openOrders()  # type: ignore[attr-defined]
        for trade in orders:
            if trade.order.orderId == order_id:  # type: ignore[attr-defined]
                self.ib.cancelOrder(trade.order)  # type: ignore[attr-defined]
                return
        raise ValueError(f"Order {order_id} not found")

    def find_stop_orders(self, symbol: str) -> list[tuple[int, float]]:
        """查找指定symbol的所有止损单，返回(order_id, stop_price)列表"""
        self.connect()
        trades = self.ib.openOrders()  # type: ignore[attr-defined]
        result: list[tuple[int, float]] = []
        for trade in trades:
            if (
                trade.contract.symbol == symbol  # type: ignore[attr-defined]
                and trade.order.orderType.upper() in ("STP", "STP LMT")  # type: ignore[attr-defined]
            ):
                order_id = trade.order.orderId  # type: ignore[attr-defined]
                stop_price = trade.order.auxPrice or trade.order.lmtPrice  # type: ignore[attr-defined]
                if stop_price:
                    result.append((order_id, float(stop_price)))
        return result

    def modify_order(self, order_id: int, stop_price: float | None = None, limit_price: float | None = None) -> OrderResponse:
        """修改现有订单的价格"""
        self.connect()
        orders = self.ib.openOrders()  # type: ignore[attr-defined]
        for trade in orders:
            if trade.order.orderId == order_id:  # type: ignore[attr-defined]
                order = trade.order
                if stop_price is not None:
                    order.auxPrice = stop_price  # type: ignore[attr-defined]
                if limit_price is not None:
                    order.lmtPrice = limit_price  # type: ignore[attr-defined]
                trade = self.ib.placeOrder(trade.contract, order)  # type: ignore[attr-defined]
                self.ib.sleep(1)
                status = trade.orderStatus.status  # type: ignore[attr-defined]
                return OrderResponse(order_id=order_id, status=status, description=str(trade.order))
        raise ValueError(f"Order {order_id} not found")

    def list_open_orders(self) -> list[dict]:
        """Return a lightweight snapshot of open orders/trades for diagnostics."""
        self.connect()
        trades = getattr(self.ib, "openTrades", lambda: [])()  # type: ignore[attr-defined]
        out: list[dict] = []
        for t in trades:
            try:
                out.append(
                    {
                        "orderId": int(t.order.orderId),
                        "permId": int(getattr(t.order, "permId", 0) or 0),
                        "symbol": getattr(t.contract, "symbol", None),
                        "status": getattr(t.orderStatus, "status", None),
                        "type": getattr(t.order, "orderType", None),
                        "action": getattr(t.order, "action", None),
                        "tif": getattr(t.order, "tif", None),
                        "parentId": getattr(t.order, "parentId", None),
                        "lmtPrice": getattr(t.order, "lmtPrice", None),
                        "auxPrice": getattr(t.order, "auxPrice", None),
                    }
                )
            except Exception:
                pass
        return out


class DryRunBroker(IBBroker):
    def __init__(self) -> None:
        self.orders: list[OrderRequest] = []

    def connect(self) -> None:  # pragma: no cover - intentionally noop
        return None

    def place_order(self, request: OrderRequest) -> OrderResponse:  # type: ignore[override]
        self.orders.append(request)
        return OrderResponse(order_id=None, status="DRY_RUN", description="Dry run order")

    def cancel_order(self, order_id: int) -> None:  # pragma: no cover - noop
        return None

    def find_stop_orders(self, symbol: str) -> list[tuple[int, float]]:  # type: ignore[override]
        return []

    def modify_order(self, order_id: int, stop_price: float | None = None, limit_price: float | None = None) -> OrderResponse:  # type: ignore[override]
        return OrderResponse(order_id=None, status="DRY_RUN", description="Dry run modify order")

    # Convenience for CLI: keep interface parity
    def place_bracket(self, parent: OrderRequest, stop: OrderRequest):  # type: ignore[override]
        self.orders.append(parent)
        self.orders.append(stop)
        return (
            OrderResponse(order_id=None, status="DRY_RUN", description="Dry run parent"),
            OrderResponse(order_id=None, status="DRY_RUN", description="Dry run stop"),
        )
