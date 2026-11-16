"""Webull OpenAPI adapter (webull-openapi-python-sdk 3.x): places real orders, no in-app confirmation.

Response field names are read defensively (`_find`) because they differ slightly between
endpoints and SDK versions. Run `python -m scripts.check_webull` once to see the raw shapes
for your account and confirm parsing before going live.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from .base import OrderResult, Quote

log = logging.getLogger(__name__)

_STATUS_MAP = {
    "SUBMITTED": "submitted", "PENDING": "submitted", "WORKING": "submitted", "NEW": "submitted",
    "PARTIAL_FILLED": "partial", "PARTIAL FILLED": "partial", "PARTIALLY_FILLED": "partial",
    "FILLED": "filled",
    "CANCELLED": "cancelled", "CANCELED": "cancelled", "EXPIRED": "cancelled",
    "FAILED": "rejected", "REJECTED": "rejected",
}


def _find(obj: Any, keys: Iterable[str]) -> Any:
    """Depth-first search for the first non-empty value under any of `keys`."""
    keys = tuple(keys)
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _find(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, keys)
            if r is not None:
                return r
    return None


def _items(obj: Any) -> list[dict]:
    """Flatten a response into the list of dict records it contains."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("data", "holdings", "positions", "items", "list", "result"):
            if isinstance(obj.get(k), list):
                return [x for x in obj[k] if isinstance(x, dict)]
        return [obj]
    return []


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class WebullOpenApiBroker:
    name = "webull_openapi"

    def __init__(self, app_key: str, app_secret: str, account_id: str, region: str = "us", endpoint: str = ""):
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient
        from webull.trade.trade_client import TradeClient

        api = ApiClient(app_key, app_secret, region)
        if endpoint:
            api.add_endpoint(region, endpoint)
        self.account_id = account_id
        self.trade = TradeClient(api)
        self.data = DataClient(api)

    @staticmethod
    def _json(res) -> Any:
        if getattr(res, "status_code", None) != 200:
            body = getattr(res, "text", res)
            raise RuntimeError(f"Webull API error {getattr(res, 'status_code', '?')}: {body}")
        return res.json()

    # -- market data --
    def get_quote(self, symbol: str) -> Quote:
        res = self.data.market_data.get_snapshot(symbol, "US_STOCK", extend_hour_required=True)
        body = self._json(res)
        rec = next((r for r in _items(body) if str(r.get("symbol", symbol)).upper() == symbol), body)
        last = _f(_find(rec, ("price", "last_price", "last", "close", "trade_price")))
        if last is None:
            raise RuntimeError(f"no price in snapshot for {symbol}: {body}")
        return Quote(symbol=symbol, last=last,
                     bid=_f(_find(rec, ("bid", "bid_price"))),
                     ask=_f(_find(rec, ("ask", "ask_price"))))

    # -- account --
    def get_position_qty(self, symbol: str) -> float:
        body = self._json(self.trade.account_v2.get_account_position(self.account_id))
        for rec in _items(body):
            sym = _find(rec, ("symbol", "ticker"))
            if sym and str(sym).upper() == symbol:
                return _f(_find(rec, ("quantity", "qty", "position", "holding_qty"))) or 0.0
        return 0.0

    def get_equity(self) -> Optional[float]:
        try:
            body = self._json(self.trade.account_v2.get_account_balance(self.account_id))
        except Exception:
            log.exception("balance lookup failed")
            return None
        return _f(_find(body, ("total_net_liquidation_value", "net_liquidation_value", "net_liquidation",
                               "total_asset", "total_assets", "total_market_value")))

    # -- orders --
    def place_limit_order(self, client_order_id, symbol, side, qty, limit_price, extended_hours):
        order = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "instrument_type": "EQUITY",
            "market": "US",
            "order_type": "LIMIT",
            "limit_price": f"{limit_price}",
            "quantity": f"{qty:g}",
            "support_trading_session": "ALL" if extended_hours else "CORE",
            "side": side,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
        }
        try:
            body = self._json(self.trade.order_v2.place_order(self.account_id, [order]))
        except Exception as e:  # network or API rejection
            return OrderResult(ok=False, client_order_id=client_order_id, status="rejected", error=str(e))
        return OrderResult(ok=True, client_order_id=client_order_id, status="submitted", raw=body)

    def get_order_status(self, client_order_id):
        try:
            body = self._json(self.trade.order_v2.get_order_detail(self.account_id, client_order_id))
        except Exception as e:
            return OrderResult(ok=False, client_order_id=client_order_id, status="unknown", error=str(e))
        raw_status = str(_find(body, ("status", "order_status")) or "").upper()
        if raw_status not in _STATUS_MAP:
            log.warning("unrecognised Webull order status %r for %s: %s", raw_status, client_order_id, body)
        return OrderResult(ok=True, client_order_id=client_order_id,
                           status=_STATUS_MAP.get(raw_status, "unknown"), raw=body)

    def cancel_order(self, client_order_id):
        try:
            body = self._json(self.trade.order_v2.cancel_order(self.account_id, client_order_id))
        except Exception as e:
            return OrderResult(ok=False, client_order_id=client_order_id, status="unknown", error=str(e))
        return OrderResult(ok=True, client_order_id=client_order_id, status="cancelled", raw=body)
