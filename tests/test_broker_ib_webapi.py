from __future__ import annotations

from types import SimpleNamespace

import builtins

from strictmode.engine.broker_ib_webapi import WebAPISessionManager, IBKRWebAPIBroker
from strictmode.engine.broker_ib import OrderRequest


class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, base_url: str = "", verify: bool = False, timeout: float = 10.0):
        self.base_url = base_url
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, **kw):
        self.calls.append(("GET", path))
        if path == "/sso/validate":
            return _Resp(200, 1)
        if path == "/tickle":
            return _Resp(200, {"tickle": True})
        if path == "/iserver/accounts":
            return _Resp(200, {"accounts": ["DU12345", "U1111"]})
        if path.startswith("/iserver/secdef/search"):
            return _Resp(200, [{"conid": 123, "symbol": "TSLA", "exchange": "NASDAQ"}])
        if path.startswith("/iserver/contract/123/info-and-rules"):
            return _Resp(200, {"exchange": "NASDAQ", "priceIncrements": [{"lowerEdge": 0, "increment": 0.01}]})
        if path.startswith("/iserver/portfolio/"):
            return _Resp(200, [])
        if path == "/iserver/account/orders":
            return _Resp(200, [])
        return _Resp(404, {})

    def post(self, path: str, json=None, **kw):
        self.calls.append(("POST", path))
        if path == "/iserver/auth/status":
            return _Resp(200, {"status": "COMPLETE"})
        if path == "/iserver/auth/ssodh/init":
            return _Resp(200, {"status": "COMPLETE"})
        if path.startswith("/iserver/account/") and path.endswith("/switch"):
            return _Resp(200, {"switched": True})
        if path.startswith("/iserver/account/") and path.endswith("/orders"):
            return _Resp(200, {"id": 9001, "status": "Submitted"})
        if path.startswith("/iserver/account/") and "/order/" in path:
            return _Resp(200, {"status": "Submitted"})
        return _Resp(404, {})

    def delete(self, path: str, **kw):
        self.calls.append(("DELETE", path))
        return _Resp(200, {"status": "Cancelled"})


def test_session_validate_and_ensure(monkeypatch):
    # Monkeypatch httpx.Client used inside session manager
    import strictmode.engine.broker_ib_webapi as m

    monkeypatch.setattr(m.httpx, "Client", _FakeClient)
    sess = WebAPISessionManager(base_url="https://127.0.0.1:5000/v1/api", verify_tls=False)
    assert sess.validate() is True
    assert sess.ensure_brokerage(compete=True) is True
    assert sess.account_id().startswith("DU")


def test_place_order_lmt_and_bracket(monkeypatch):
    import strictmode.engine.broker_ib_webapi as m

    monkeypatch.setattr(m.httpx, "Client", _FakeClient)
    sess = WebAPISessionManager(base_url="https://127.0.0.1:5000/v1/api", verify_tls=False)
    broker = IBKRWebAPIBroker(session=sess, paper=True)

    parent = OrderRequest(symbol="TSLA", qty=10, side="BUY", order_type="LMT", limit_price=200.12, tif="GTC", order_ref="SM:TSLA")
    stop = OrderRequest(symbol="TSLA", qty=10, side="SELL", order_type="STP", stop_price=180.0, tif="GTC", order_ref="SM:TSLA")
    p, s = broker.place_bracket(parent, stop)
    assert p.status.upper().startswith("SUB")
    assert isinstance(s.status, str)
