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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify = verify_tls
        self.heartbeat_sec = heartbeat_sec
        self.account_hint = account_hint
        self._client = httpx.Client(base_url=self.base_url, verify=self.verify, timeout=timeout)
        self._last_tick = 0.0
        self._account_id: str | None = None

    # --- HTTP helpers -----------------------------------------------------
    def _get(self, path: str, **kw) -> httpx.Response:
        return self._client.get(path, **kw)

    def _post(self, path: str, json: Any | None = None, **kw) -> httpx.Response:
        return self._client.post(path, json=json, **kw)

    def _delete(self, path: str, **kw) -> httpx.Response:
        return self._client.delete(path, **kw)

    # --- Session primitives ----------------------------------------------
    def validate(self) -> bool:
        """Validate read-only SSO session and cache accounts if possible."""
        try:
            r = self._get("/sso/validate")
            if r.status_code != 200:
                return False
            data = r.json()
            valid = False
            # Some deployments return {"VALID": true} or numeric 1
            if isinstance(data, dict):
                valid = bool(data.get("VALID") or data.get("valid") or data.get("isAuthenticated"))
            elif isinstance(data, (int, float, str)):
                valid = str(data).strip() in {"1", "true", "True"}
            if not valid:
                return False
        except Exception:
            return False
        # Probe auth status for diagnostics but do not fail on errors
        try:
            self._post("/iserver/auth/status")
        except Exception:
            pass
        return True

    def ensure_brokerage(self, compete: bool = True) -> bool:
        """Ensure brokerage (trade) session is ready; optionally reclaim (compete)."""
        try:
            payload = {"publish": True, "compete": bool(compete)}
            r = self._post("/iserver/auth/ssodh/init", json=payload)
            if r.status_code not in (200, 202):
                return False
        except Exception:
            return False
        # Probe accounts and cache selection
        try:
            self._select_account()
        except Exception:
            # Not fatal; caller may pass explicit account later
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
        r = self._get("/iserver/accounts")
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
        payload: dict[str, Any] = {
            "conid": c.conid,
            "side": side,
            "orderType": ot,
            "tif": request.tif,
            "quantity": abs(request.qty),
            "outsideRTH": bool(request.outside_rth),
        }
        if request.order_ref:
            payload["cOID"] = request.order_ref
        if price is not None:
            payload["price"] = price

        r = self.session._post(f"/iserver/account/{acct}/orders", json=payload)
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
            "outsideRTH": bool(stop.outside_rth),
            "price": price,
        }
        if stop.order_ref:
            payload["cOID"] = stop.order_ref
        if stop.parent_id is not None:
            payload["parentId"] = int(stop.parent_id)
        r = self.session._post(f"/iserver/account/{acct}/orders", json=payload)
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
        if not isinstance(data, list):
            return out
        for o in data:
            if not isinstance(o, dict):
                continue
            try:
                out.append(
                    {
                        "orderId": int(o.get("orderId") or o.get("id") or 0),
                        "symbol": o.get("ticker") or o.get("symbol"),
                        "status": o.get("status"),
                        "type": o.get("orderType"),
                        "action": o.get("side"),
                        "tif": o.get("tif"),
                        "parentId": o.get("parentId"),
                        "orderRef": o.get("cOID"),
                        "totalQuantity": o.get("quantity"),
                        "lmtPrice": o.get("lmtPrice") or o.get("price") if o.get("orderType") == "LMT" else None,
                        "auxPrice": o.get("auxPrice") or o.get("price") if o.get("orderType") in ("STP", "STOP") else None,
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
        r = self.session._get(f"/iserver/portfolio/{acct}/positions")
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
                sym = p.get("ticker") or p.get("symbol")
                exch = str(p.get("exchange") or "").upper()
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

