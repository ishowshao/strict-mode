from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    from ib_insync import IB, Contract, Order
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

    def _contract(self, symbol: str) -> Contract:
        from ib_insync import Stock

        return Stock(symbol, "SMART", "USD")

    def place_order(self, request: OrderRequest) -> OrderResponse:
        self.connect()
        contract = self._contract(request.symbol)
        from ib_insync import Order

        order = Order(
            action="BUY" if request.side.upper() == "BUY" else "SELL",
            orderType=request.order_type.upper(),
            totalQuantity=abs(request.qty),
            tif=request.tif,
            lmtPrice=request.limit_price,
            auxPrice=request.stop_price,
            outsideRth=not request.outside_rth,
        )
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)
        status = trade.orderStatus.status
        order_id = trade.order.orderId
        return OrderResponse(order_id=order_id, status=status, description=str(trade.order))

    def cancel_order(self, order_id: int) -> None:
        self.connect()
        self.ib.cancelOrder(self.ib.orders()[order_id])  # type: ignore[index]


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
