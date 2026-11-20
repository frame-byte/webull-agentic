"""Paper broker: never touches a real account.

Quotes can come from a real broker (so dry runs use live prices) or a fixed map (tests).
Positions are tracked in memory as if every order filled instantly at its limit price.
"""
from __future__ import annotations

from typing import Optional

from .base import OrderResult, Quote


class DryRunBroker:
    name = "dryrun"

    def __init__(self, quote_source=None, prices: Optional[dict[str, float]] = None, equity: float = 100_000):
        self._quote_source = quote_source
        self._prices = prices or {}
        self.positions: dict[str, float] = {}
        self.equity = equity
        self.orders: list[dict] = []
        self.stops: dict[str, dict] = {}   # client_order_id -> {symbol, qty, trail_pct, status}

    def get_quote(self, symbol: str) -> Quote:
        if self._quote_source is not None:
            return self._quote_source.get_quote(symbol)
        p = self._prices[symbol]
        return Quote(symbol=symbol, last=p, bid=p * 0.9995, ask=p * 1.0005)

    def get_position_qty(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def get_equity(self) -> Optional[float]:
        return self.equity

    def place_limit_order(self, client_order_id, symbol, side, qty, limit_price, extended_hours):
        sign = 1 if side == "BUY" else -1
        self.positions[symbol] = self.positions.get(symbol, 0.0) + sign * qty
        o = dict(client_order_id=client_order_id, symbol=symbol, side=side, qty=qty, limit_price=limit_price)
        self.orders.append(o)
        return OrderResult(ok=True, client_order_id=client_order_id, status="dryrun", raw=o)

    def place_trailing_stop(self, client_order_id, symbol, qty, trail_pct):
        self.stops[client_order_id] = dict(symbol=symbol, qty=qty, trail_pct=trail_pct, status="submitted")
        return OrderResult(ok=True, client_order_id=client_order_id, status="submitted",
                           raw=self.stops[client_order_id])

    def trigger_stop(self, client_order_id):
        """Test/dry-run helper: simulate the stop being hit."""
        st = self.stops[client_order_id]
        st["status"] = "filled"
        self.positions[st["symbol"]] = self.positions.get(st["symbol"], 0.0) - st["qty"]

    def get_order_status(self, client_order_id):
        if client_order_id in self.stops:
            return OrderResult(ok=True, client_order_id=client_order_id, status=self.stops[client_order_id]["status"])
        return OrderResult(ok=True, client_order_id=client_order_id, status="dryrun")

    def cancel_order(self, client_order_id):
        st = self.stops.get(client_order_id)
        if st is not None and st["status"] != "submitted":
            return OrderResult(ok=False, client_order_id=client_order_id, status="unknown",
                               error=f"order already {st['status']}")
        if st is not None:
            st["status"] = "cancelled"
        return OrderResult(ok=True, client_order_id=client_order_id, status="cancelled")
