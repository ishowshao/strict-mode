from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .broker_ib import OrderRequest, OrderResponse


class WebAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class _Contract:
    conid: int
    symbol: str
    exchange: str | None = None
    currency: str | None = None
    rules: dict | None = None


class WebAPISessionManager:
    """Thin session helper for IBKR Web API (Client Portal Gateway).

    Provides validate(), ensure_brokerage(), heartbeat() primitives per tech-spec.
    """

    def __init__(
        self,
        base_url: str,
        verify_tls: bool = False,
        heartbeat_sec: int = 45,
        account_hint: str | None = None,
        timeout: float = 10.0,
        trust_env: bool = False,
    ) -> None:
        # Ensure base_url ends with '/' so relative paths append after /v1/api/
        self.base_url = base_url if base_url.endswith('/') else base_url + '/'
        self.verify = verify_tls
        self.heartbeat_sec = heartbeat_sec
        self.account_hint = account_hint
        # Avoid inheriting proxy env which may cause localhost calls to time out
        self._client = httpx.Client(
            base_url=self.base_url,
            verify=self.verify,
            timeout=httpx.Timeout(timeout),
            trust_env=trust_env,
        )
        self._last_tick = 0.0
        self._account_id: str | None = None

    # --- HTTP helpers -----------------------------------------------------
    def _get(self, path: str, **kw) -> httpx.Response:
        # With httpx base_url set to .../v1/api, absolute paths would drop the base path
        # Normalize to relative paths so final URL is base_url + path
        return self._client.get(path.lstrip("/"), **kw)

    def _post(self, path: str, json: Any | None = None, **kw) -> httpx.Response:
        return self._client.post(path.lstrip("/"), json=json, **kw)

    def _delete(self, path: str, **kw) -> httpx.Response:
        return self._client.delete(path.lstrip("/"), **kw)

    # --- Session primitives ----------------------------------------------
    def validate(self) -> bool:
        """Validate read-only SSO session and cache accounts if possible.

        Different Gateway builds return different shapes, e.g.:
        - 1 / "1" / true
        - {"VALID": true}
        - {"RESULT": true, "EXPIRES": 12345, ...}
        Treat any truthy VALID/RESULT/isauthenticated or positive EXPIRES as valid.
        """
        try:
            # Prime cookie/session
            try:
                self._post("/iserver/auth/status")
            except Exception:
                pass
            try:
                self._get("/tickle")
            except Exception:
                pass
            r = self._get("/sso/validate")
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    data = None
                valid = False
                if isinstance(data, dict):
                    low = {str(k).lower(): v for k, v in data.items()}
                    valid = bool(
                        low.get("valid")
                        or low.get("result")
                        or low.get("isauthenticated")
                        or low.get("is_authenticated")
                        or low.get("authenticated")
                    )
                    if not valid:
                        # Fallback heuristic: EXPIRES positive suggests active session
                        try:
                            exp = float(low.get("expires") or low.get("expiry") or 0)
                            if exp > 0:
                                valid = True
                        except Exception:
                            pass
                elif isinstance(data, (int, float, str, bool)):
                    s = str(data).strip().lower()
                    valid = s in {"1", "true", "ok"}
                if valid:
                    return True
        except Exception:
            pass
        # Fallback: use auth/status shape as read-only session indicator
        try:
            rs = self._post("/iserver/auth/status")
            if rs.status_code == 200:
                body = rs.json() or {}
                if isinstance(body, dict):
                    low = {str(k).lower(): v for k, v in body.items()}
                    a = low.get("authenticated")
                    status = str(low.get("status") or "").upper()
                    if a is True or status == "COMPLETE":
                        return True
        except Exception:
            pass
        return False
        # Probe auth status for diagnostics but do not fail on errors
        try:
            self._post("/iserver/auth/status")
        except Exception:
            pass
        return True

    def ensure_brokerage(self, compete: bool = True) -> bool:
        """Ensure brokerage (trade) session is ready; optionally reclaim (compete).

        After init, poll /iserver/auth/status until authenticated/connected or COMPLETE.
        """
        try:
            payload = {"publish": True, "compete": bool(compete)}
            # Prime cookie/session
            try:
                self._post("/iserver/auth/status")
                self._get("/tickle")
            except Exception:
                pass
            r = self._post("/iserver/auth/ssodh/init", json=payload)
            if r.status_code not in (200, 202):
                # Try legacy reauth then retry ssodh once
                try:
                    self._post("/iserver/reauthenticate")
                    time.sleep(0.5)
                    r = self._post("/iserver/auth/ssodh/init", json=payload)
                except Exception:
                    pass
            if r.status_code not in (200, 202):
                return False
            # If response body indicates failure, attempt legacy reauth once more
            try:
                body = r.json() or {}
                if isinstance(body, dict) and "failed" in str(body).lower():
                    self._post("/iserver/reauthenticate")
                    time.sleep(0.5)
            except Exception:
                pass
        except Exception:
            return False
        # Poll auth/status for readiness
        ready = False
        for _ in range(20):  # ~6s max
            try:
                rs = self._post("/iserver/auth/status")
                if rs.status_code == 200:
                    body = rs.json() or {}
                    if isinstance(body, dict):
                        low = {str(k).lower(): v for k, v in body.items()}
                        if low.get("authenticated") is True or low.get("connected") is True or str(low.get("status") or "").upper() == "COMPLETE":
                            ready = True
                            break
                # Secondary signal: marketdata connected endpoint
                rc = self._get("/iserver/marketdata/connected")
                if rc.status_code == 200:
                    try:
                        if bool(rc.json()):
                            ready = True
                            break
                    except Exception:
                        pass
                time.sleep(0.3)
            except Exception:
                time.sleep(0.3)
        if not ready:
            return False
        # Probe accounts and cache selection
        try:
            self._select_account()
        except Exception:
            pass
        return True

    def heartbeat(self) -> bool:
        now = time.time()
        if now - self._last_tick < self.heartbeat_sec * 0.5:
            return True
        try:
            r = self._get("/tickle")
            ok = r.status_code == 200
            if ok:
                self._last_tick = now
            return ok
        except Exception:
            return False

    # --- Account selection ------------------------------------------------
    def _select_account(self) -> str:
        # Prime local session cookie and gateway session state
        try:
            self._get("/tickle")
        except Exception:
            pass
        r = self._get("/iserver/accounts")
        if r.status_code == 400 or r.status_code == 401:
            # Likely missing brokerage session; try to init and retry a few times
            self.ensure_brokerage(compete=True)
            for _ in range(5):
                time.sleep(0.2)
                try:
                    r = self._get("/iserver/accounts")
                    if r.status_code == 200:
                        break
                except Exception:
                    pass
            # Fallback path observed in docs: validate SSO then reauthenticate
            if r.status_code != 200:
                try:
                    self._get("/sso/validate")
                except Exception:
                    pass
                try:
                    rr = self._post("/iserver/reauthenticate", json={})
                    if rr.status_code in (200, 202):
                        time.sleep(0.5)
                        r = self._get("/iserver/accounts")
                except Exception:
                    pass
        r.raise_for_status()
        data = r.json() or {}
        accounts = []
        if isinstance(data, dict) and "accounts" in data:
            accounts = data.get("accounts", [])
        elif isinstance(data, list):
            accounts = data
        # Normalize to list of ids
        ids: list[str] = []
        for a in accounts:
            if isinstance(a, str):
                ids.append(a)
            elif isinstance(a, dict):
                aid = a.get("id") or a.get("accountId") or a.get("account")
                if aid:
                    ids.append(str(aid))
        if not ids:
            raise WebAPIError("No accounts returned by /iserver/accounts")
        # Pick based on hint or DU/U prefix
        hint = (self.account_hint or "").strip().upper()
        choice = None
        if hint and len(hint) >= 2 and hint in {"DU", "U"}:
            for aid in ids:
                if aid.upper().startswith(hint):
                    choice = aid
                    break
        elif hint and len(hint) > 2:
            for aid in ids:
                if aid.upper() == hint:
                    choice = aid
                    break
        if choice is None:
            # Prefer paper over live by default to be safe
            for aid in ids:
                if str(aid).upper().startswith("DU"):
                    choice = aid
                    break
            if choice is None:
                choice = ids[0]
        # Switch if required (best-effort)
        try:
            self._post(f"/iserver/account/{choice}/switch")
        except Exception:
            pass
        self._account_id = str(choice)
        return self._account_id

    def account_id(self) -> str:
        if self._account_id:
            return self._account_id
        return self._select_account()


class IBKRWebAPIBroker:
    """Synchronous client compatible with IBBroker interface using Web API.

    Methods mirror engine.broker_ib.IBBroker where reasonable so CLI can swap.
    """

    def __init__(self, session: WebAPISessionManager, paper: bool = True) -> None:
        self.session = session
        self.paper = paper

    # --- Contract helpers -------------------------------------------------
    def _hk_like(self, symbol: str) -> bool:
        return str(symbol).upper().endswith(".HK")

    def _normalize_symbol_for_search(self, symbol: str) -> str:
        s = str(symbol).strip().upper()
        if s.endswith('.HK'):
            core = "".join(ch for ch in s[:-3] if ch.isdigit())
            return core.lstrip('0') or "0"
        return s

    def _resolve_contract(self, symbol: str, currency: str | None) -> _Contract:
        sym = self._normalize_symbol_for_search(symbol)
        # 1) search
        r = self.session._get(f"/iserver/secdef/search", params={"symbol": sym})
        r.raise_for_status()
        arr = r.json() or []
        if not isinstance(arr, list):
            arr = []
        # choose SEHK for HK; otherwise prefer SMART/primary exchanges
        preferred_exchs = ["SEHK"] if self._hk_like(symbol) else ["SMART", "NASDAQ", "NYSE", "ARCA"]
        chosen: Optional[dict] = None
        for ex in preferred_exchs:
            for it in arr:
                if not isinstance(it, dict):
                    continue
                ex_code = str(it.get("exchange") or it.get("primaryExchange") or "").upper()
                if ex_code == ex and str(it.get("symbol") or "").upper() == sym:
                    chosen = it
                    break
            if chosen:
                break
        if not chosen and arr:
            # fallback to first
            chosen = next((it for it in arr if isinstance(it, dict)), None)
        if not chosen:
            raise WebAPIError(f"No contract candidates for {symbol}")
        conid = int(chosen.get("conid") or chosen.get("conId") or 0)
        if not conid:
            raise WebAPIError("Missing conid from search result")
        # 2) info and rules
        r2 = self.session._get(f"/iserver/contract/{conid}/info-and-rules")
        r2.raise_for_status()
        info = r2.json() or {}
        exch = str(info.get("exchange") or chosen.get("exchange") or "").upper() or None
        cur = currency or info.get("currency") or chosen.get("currency")
        return _Contract(conid=conid, symbol=symbol, exchange=exch, currency=cur, rules=info)

    def _min_tick(self, rules: dict | None, price: float) -> float:
        if not rules:
            return 0.01
        try:
            incs = rules.get("priceIncrements") or []
            last = 0.01
            for inc in incs:
                low = float(inc.get("lowerEdge", 0.0))
                if price >= low:
                    last = float(inc.get("increment", last))
                else:
                    break
            return max(last, 0.0001)
        except Exception:
            return 0.01

    # --- Core actions -----------------------------------------------------
    def place_order(self, request: OrderRequest) -> OrderResponse:
        # Ensure sessions
        if not self.session.validate():
            raise WebAPIError("Read-only session invalid; please login to Gateway")
        if not self.session.ensure_brokerage(compete=True):
            raise WebAPIError("Failed to establish brokerage session")
        acct = self.session.account_id()

        # Resolve contract and price rounding
        c = self._resolve_contract(request.symbol, request.currency)
        px = request.limit_price if request.order_type.upper() == "LMT" else request.stop_price
        inc = self._min_tick(c.rules, float(px or 0.0))
        if request.order_type.upper() == "LMT" and request.limit_price is not None:
            price = round(round(float(request.limit_price) / inc) * inc, 6)
        elif request.order_type.upper() in {"STP", "STOP"} and request.stop_price is not None:
            price = round(round(float(request.stop_price) / inc) * inc, 6)
        else:
            price = None

        # Build order payload for CP API
        side = "BUY" if request.side.upper() == "BUY" else "SELL"
        ot = request.order_type.upper()
        if ot == "STP":
            ot = "STOP"
        # Align TIF: IBKR Web API rejects MKT+GTC, use DAY for market orders
        tif = request.tif
        if ot == "MKT" and str(tif).upper() == "GTC":
            tif = "DAY"
        payload: dict[str, Any] = {
            "conid": c.conid,
            "side": side,
            "orderType": ot,
            "tif": tif,
            "quantity": abs(request.qty),
            "outsideRth": bool(request.outside_rth),
        }
        if request.order_ref:
            payload["cOID"] = request.order_ref
        if price is not None:
            payload["price"] = price

        r = self.session._post(f"/iserver/account/{acct}/orders", json={"orders": [payload]})
        r.raise_for_status()
        resp = r.json() or {}
        # Response could be object or list; attempt to extract id + status
        oid: Optional[int] = None
        status = "SUBMITTED"
        if isinstance(resp, dict):
            oid = int(resp.get("id") or resp.get("order_id") or resp.get("orderId") or 0) or None
            status = str(resp.get("status") or status)
        elif isinstance(resp, list) and resp:
            first = resp[0]
            if isinstance(first, dict):
                oid = int(first.get("id") or first.get("orderId") or first.get("order_id") or 0) or None
                status = str(first.get("status") or status)
        return OrderResponse(order_id=oid, status=status, description=str(resp))

    def place_bracket(self, parent: OrderRequest, stop: OrderRequest) -> tuple[OrderResponse, OrderResponse]:
        # Stage parent then child with parentId
        parent.transmit = True  # CP API transmits immediately per request
        p = self.place_order(parent)
        parent_id = p.order_id
        if parent_id is None:
            # As a fallback, try to discover from open orders
            try:
                olist = self.list_open_orders()
                if olist:
                    parent_id = int(olist[-1].get("orderId"))
            except Exception:
                pass
        stop.parent_id = parent_id
        # CP API expects child linkage via parentId
        # We'll send child with cOID and parentId
        stop.order_ref = stop.order_ref or parent.order_ref
        return p, self._place_child_stop(stop)

    def _place_child_stop(self, stop: OrderRequest) -> OrderResponse:
        if not self.session.validate():
            raise WebAPIError("Read-only session invalid; please login to Gateway")
        if not self.session.ensure_brokerage(compete=True):
            raise WebAPIError("Failed to establish brokerage session")
        acct = self.session.account_id()
        c = self._resolve_contract(stop.symbol, stop.currency)
        inc = self._min_tick(c.rules, float(stop.stop_price or 0.0))
        price = round(round(float(stop.stop_price or 0.0) / inc) * inc, 6)
        payload: dict[str, Any] = {
            "conid": c.conid,
            "side": "SELL",
            "orderType": "STOP",
            "tif": stop.tif,
            "quantity": abs(stop.qty),
            "outsideRth": bool(stop.outside_rth),
            "price": price,
        }
        if stop.order_ref:
            payload["cOID"] = stop.order_ref
        if stop.parent_id is not None:
            payload["parentId"] = int(stop.parent_id)
        r = self.session._post(f"/iserver/account/{acct}/orders", json={"orders": [payload]})
        r.raise_for_status()
        resp = r.json() or {}
        oid: Optional[int] = None
        status = "SUBMITTED"
        if isinstance(resp, dict):
            oid = int(resp.get("id") or resp.get("order_id") or resp.get("orderId") or 0) or None
            status = str(resp.get("status") or status)
        elif isinstance(resp, list) and resp:
            first = resp[0]
            if isinstance(first, dict):
                oid = int(first.get("id") or first.get("orderId") or first.get("order_id") or 0) or None
                status = str(first.get("status") or status)
        return OrderResponse(order_id=oid, status=status, description=str(resp))

    def cancel_order(self, order_id: int) -> None:
        acct = self.session.account_id()
        r = self.session._delete(f"/iserver/account/{acct}/order/{int(order_id)}")
        r.raise_for_status()

    def modify_order(self, order_id: int, stop_price: float | None = None, limit_price: float | None = None) -> OrderResponse:
        acct = self.session.account_id()
        fields: dict[str, Any] = {}
        if stop_price is not None:
            fields["price"] = float(stop_price)
            fields["orderType"] = "STOP"
        if limit_price is not None:
            fields["price"] = float(limit_price)
            fields["orderType"] = "LMT"
        r = self.session._post(f"/iserver/account/{acct}/order/{int(order_id)}", json=fields)
        r.raise_for_status()
        data = r.json() or {}
        status = str(data.get("status") or "SUBMITTED") if isinstance(data, dict) else "SUBMITTED"
        return OrderResponse(order_id=order_id, status=status, description=str(data))

    # --- Introspection helpers -------------------------------------------
    def list_open_orders(self) -> list[dict]:
        r = self.session._get("/iserver/account/orders")
        if r.status_code != 200:
            return []
        data = r.json() or []
        out: list[dict] = []
        # CP API may return {"orders": [...], "snapshot": true}
        if isinstance(data, dict) and "orders" in data:
            items = data.get("orders") or []
        else:
            items = data
        if not isinstance(items, list):
            return out
        for o in items:
            if not isinstance(o, dict):
                continue
            try:
                # Normalize fields observed in CP API responses
                tif = o.get("tif") or o.get("timeInForce")
                order_ref = o.get("cOID") or o.get("order_ref")
                qty = o.get("quantity") or o.get("totalSize") or o.get("totalQuantity")
                aux = o.get("auxPrice") or o.get("stop_price")
                lmt = o.get("lmtPrice") or (o.get("price") if str(o.get("orderType")).upper() in {"LMT", "LIMIT"} else None)
                # Cast string numbers to float when needed
                def _to_float(v):
                    try:
                        return float(v)
                    except Exception:
                        return None
                out.append(
                    {
                        "orderId": int(o.get("orderId") or o.get("id") or 0),
                        "symbol": o.get("ticker") or o.get("symbol"),
                        "status": o.get("status"),
                        "type": o.get("orderType") or o.get("origOrderType"),
                        "action": o.get("side"),
                        "tif": tif,
                        "parentId": o.get("parentId"),
                        "orderRef": order_ref,
                        "totalQuantity": _to_float(qty),
                        "lmtPrice": _to_float(lmt),
                        "auxPrice": _to_float(aux),
                    }
                )
            except Exception:
                continue
        return out

    def list_completed_orders(self, api_only: bool = True) -> list[dict]:  # api_only kept for parity
        # Web API exposes combined orders endpoint; return empty if unsupported
        try:
            return []
        except Exception:
            return []

    def list_positions(self) -> list[dict]:
        acct = self.session.account_id()
        # CP API positions are under /portfolio, not /iserver
        r = self.session._get(f"/portfolio/{acct}/positions")
        if r.status_code != 200:
            return []
        data = r.json() or []
        out: list[dict] = []
        if not isinstance(data, list):
            return out
        for p in data:
            if not isinstance(p, dict):
                continue
            try:
                sym = p.get("ticker") or p.get("symbol") or p.get("contractDesc")
                exch = str(p.get("exchange") or p.get("listingExchange") or "").upper()
                if exch == "SEHK" and isinstance(sym, str) and sym.isdigit():
                    sym = sym.zfill(4) + ".HK"
                out.append(
                    {
                        "symbol": sym,
                        "qty": float(p.get("position") or p.get("qty") or 0.0),
                        "avgCost": float(p.get("avgCost") or p.get("averageCost") or 0.0) or None,
                        "currency": p.get("currency"),
                    }
                )
            except Exception:
                continue
        return out

    # --- Utilities used by daily_task/CLI --------------------------------
    def find_stop_orders(self, symbol: str, order_ref_prefix: str | None = None) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        sym_u = str(symbol).upper().strip()
        def _match_sym(s: Optional[str]) -> bool:
            if not s:
                return False
            if sym_u.endswith('.HK') and s.isdigit():
                return s == sym_u[:-3].lstrip('0')
            return str(s).upper() == sym_u
        for o in self.list_open_orders():
            try:
                if str(o.get("type") or "").upper() in {"STP", "STOP", "STP LMT"} and _match_sym(o.get("symbol")):
                    if order_ref_prefix:
                        ref = o.get("orderRef")
                        if not (isinstance(ref, str) and ref.startswith(order_ref_prefix)):
                            continue
                    price = o.get("auxPrice") or o.get("lmtPrice") or o.get("price")
                    if price is not None:
                        out.append((int(o.get("orderId") or 0), float(price)))
            except Exception:
                continue
        return out
