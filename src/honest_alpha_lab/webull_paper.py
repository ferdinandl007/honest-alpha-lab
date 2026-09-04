"""Webull HK: sandbox, explicitly armed live orders, and read-only market data.

Production order access requires a separate deployment arming variable. HTTP signing
follows Webull's official protocol; redirects and automatic POST retries are off.
Credentials are environment-only and never included in reports or exception text.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .contracts import ContractError, canonical_hash

SANDBOX_HOST = "api.sandbox.webull.hk"
DATA_HOST = "api.webull.hk"
LIVE_ARMING = "YES_I_ACCEPT_REAL_ORDERS"
PLACE = "/openapi/trade/order/place"
DETAIL = "/openapi/trade/order/detail"
BARS = "/openapi/market-data/stock/bars"
SNAPSHOT = "/openapi/market-data/stock/snapshot"


def signed_headers(path, query, body, *, app_key, app_secret, host, token,
                   timestamp=None, nonce=None):
    headers = {"x-app-key": app_key, "x-timestamp": timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "x-signature-algorithm": "HMAC-SHA1", "x-signature-version": "1.0",
               "x-signature-nonce": nonce or uuid4().hex, "host": host}
    parts = {**query, **headers}
    message = path + "&" + "&".join(f"{key}={parts[key]}" for key in sorted(parts))
    if body:
        message += "&" + hashlib.md5(body, usedforsecurity=False).hexdigest().upper()
    signature = hmac.new((app_secret + "&").encode(), quote(message, safe="").encode(), hashlib.sha1).digest()
    return {**headers, "x-signature": base64.b64encode(signature).decode(),
            "x-version": "v2", "x-access-token": token, "Content-Type": "application/json"}


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError("Webull redirects are disabled")


def _http(method, host, path, query, body, headers):
    url = "https://" + host + path + ("?" + urlencode(query) if query else "")
    request = Request(url, data=body or None, headers=headers, method=method)
    try:
        with build_opener(_NoRedirects()).open(request, timeout=15) as response:
            content = response.read(8_000_001)
            if len(content) > 8_000_000:
                raise ContractError("Webull response exceeds byte budget")
            return json.loads(content)
    except HTTPError as exc:
        raise ContractError(f"Webull HTTP {exc.code}; inspect account access separately") from None
    except (URLError, TimeoutError, ValueError):
        raise ContractError("Webull request failed or returned invalid JSON") from None


class WebullHKClient:
    """Fixed hosts. Live access is distinct from sandbox and read-only data."""
    def __init__(self, *, mode="sandbox", transport=None):
        if mode not in {"sandbox", "data_only", "live"}:
            raise ContractError("unsupported Webull mode")
        if mode == "live" and os.environ.get("HAL_ENABLE_LIVE_TRADING") != LIVE_ARMING:
            raise ContractError("live trading is disabled at deployment level")
        self._mode = mode
        self._transport = transport or _http

    @property
    def host(self):
        return SANDBOX_HOST if self._mode == "sandbox" else DATA_HOST

    def _request(self, method, path, query=None, payload=None):
        if self._mode == "live" and os.environ.get("HAL_ENABLE_LIVE_TRADING") != LIVE_ARMING:
            raise ContractError("live trading is disabled at deployment level")
        data_paths = {BARS, SNAPSHOT}
        if not (method == "GET" and path in data_paths or self._mode in {"sandbox", "live"} and
                ((method == "POST" and path == PLACE) or (method == "GET" and path == DETAIL))):
            raise ContractError("request is outside the paper/read-only endpoint allowlist")
        prefix = {"sandbox": "HAL_WEBULL_SANDBOX_", "data_only": "HAL_WEBULL_DATA_", "live": "HAL_WEBULL_LIVE_"}[self._mode]
        values = [os.environ.get(prefix + field) for field in ("APP_KEY", "APP_SECRET", "ACCESS_TOKEN")]
        if not all(values):
            raise ContractError(f"configure {prefix}APP_KEY, APP_SECRET and ACCESS_TOKEN outside agents")
        query = query or {}
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode() if payload else b""
        headers = signed_headers(path, query, body, app_key=values[0], app_secret=values[1],
                                 token=values[2], host=self.host)
        try:
            return self._transport(method, self.host, path, query, body, headers)
        except ContractError:
            raise
        except Exception:  # noqa: BLE001 -- no credential-bearing transport errors escape
            raise ContractError("Webull transport failed; submission outcome may be unknown") from None

    def history(self, symbol, *, category="US_STOCK", count=200):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,20}", symbol):
            raise ContractError("invalid provider symbol")
        if category not in {"US_STOCK", "HK_STOCK"} or type(count) is not int or not 1 <= count <= 1200:
            raise ContractError("unsupported category or bar count")
        return self._request("GET", BARS, {"symbol": symbol, "category": category,
                                         "timespan": "D", "count": str(count)})

    def snapshot(self, symbol, *, category="US_STOCK"):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,20}", symbol):
            raise ContractError("invalid provider symbol")
        if category not in {"US_STOCK", "HK_STOCK"}:
            raise ContractError("unsupported category")
        return self._request("GET", SNAPSHOT, {"symbols": symbol, "category": category})

    def order_detail(self, account_id, client_order_id):
        return self._request("GET", DETAIL, {"account_id": account_id, "client_order_id": client_order_id})


class SandboxOrderJournal:
    """At-most-one submission attempt per immutable intent; ambiguous stays unknown.

    Retry does not mean resubmit. Query the broker by the same client ID to
    reconcile unknown outcomes. This intentionally favors no duplicate orders.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, response TEXT)")

    def submit(self, client, *, account_id, client_order_id, symbol, side, quantity,
               limit_price, max_notional=1000, enable_sandbox_orders=False,
               enable_live_orders=False):
        sandbox = client._mode == "sandbox" and client.host == SANDBOX_HOST and enable_sandbox_orders
        live = (client._mode == "live" and client.host == DATA_HOST and enable_live_orders
                and os.environ.get("HAL_ENABLE_LIVE_TRADING") == LIVE_ARMING)
        if not (sandbox or live):
            raise ContractError("order submission requires explicit matching environment enablement")
        if (not isinstance(client_order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client_order_id)
                or not isinstance(account_id, str) or not account_id
                or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", symbol)
                or side not in {"BUY", "SELL"}):
            raise ContractError("invalid US equity paper order identity or side")
        try:
            qty, price, cap = (Decimal(str(value)) for value in (quantity, limit_price, max_notional))
            if (not all(value.is_finite() and value > 0 for value in (qty, price, cap))
                    or qty != qty.to_integral_value() or qty * price > cap):
                raise ContractError("paper limit order exceeds positive whole-share/notional constraints")
        except InvalidOperation:
            raise ContractError("invalid decimal order values") from None
        order = {"combo_type": "NORMAL", "client_order_id": client_order_id,
                 "symbol": symbol, "instrument_type": "EQUITY", "market": "US",
                 "order_type": "LIMIT", "limit_price": str(price), "quantity": str(qty),
                 "support_trading_session": "CORE", "side": side, "time_in_force": "DAY",
                 "entrust_type": "QTY"}
        payload = {"account_id": account_id, "new_orders": [order]}
        digest = canonical_hash({"host": client.host, "payload": payload})
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT digest,state,response FROM intents WHERE id=?", (client_order_id,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ContractError("paper order ID cannot be reused for changed intent")
                return {"state": previous[1], "response": json.loads(previous[2]) if previous[2] else None}
            db.execute("INSERT INTO intents VALUES(?,?,?,NULL)", (client_order_id, digest, "unknown"))
        response = client._request("POST", PLACE, payload=payload)
        # HTTP success is not a fill or even an order acceptance claim.
        encoded = json.dumps(response, allow_nan=False)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE intents SET state='response_received',response=? WHERE id=?", (encoded, client_order_id))
        return {"state": "response_received", "response": response}
