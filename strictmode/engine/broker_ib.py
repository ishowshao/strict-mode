from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


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

    @property
    def ib(self) -> IB:
        if self._ib is None:
            self._ib = IB()
        return self._ib

    def connect(self) -> None:
        if not self.ib.isConnected():  # type: ignore[attr-defined]
            self.ib.connect(self.host, self.port, clientId=self.client_id)

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():  # type: ignore[attr-defined]
            self._ib.disconnect()

    def _contract(self, symbol: str, currency: str) -> Contract:
        from ib_insync import Stock

        return Stock(symbol, "SMART", currency)

    def place_order(self, request: OrderRequest) -> OrderResponse:
        self.connect()
        contract = self._contract(request.symbol, request.currency)
        from ib_insync import Order

        order = Order(
            action="BUY" if request.side.upper() == "BUY" else "SELL",
            orderType=request.order_type.upper(),
            totalQuantity=abs(request.qty),
            tif=request.tif,
            lmtPrice=request.limit_price,
            auxPrice=request.stop_price,
            outsideRth=request.outside_rth,
        )
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)
        status = trade.orderStatus.status
        order_id = trade.order.orderId
        return OrderResponse(order_id=order_id, status=status, description=str(trade.order))

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
