from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Quote:
    symbol: str
    last: float
    bid: Optional[float] = None
    ask: Optional[float] = None


@dataclass
class OrderResult:
    ok: bool
    client_order_id: str
    status: str            # submitted | filled | partial | cancelled | rejected | dryrun | unknown
    raw: object = None
    error: str = ""


class Broker(Protocol):
    name: str

    def get_quote(self, symbol: str) -> Quote: ...
    def get_position_qty(self, symbol: str) -> float: ...
    def get_equity(self) -> Optional[float]: ...
    def place_limit_order(self, client_order_id: str, symbol: str, side: str, qty: float,
                          limit_price: float, extended_hours: bool) -> OrderResult: ...
    def place_trailing_stop(self, client_order_id: str, symbol: str, qty: float,
                            trail_pct: float) -> OrderResult: ...
    def get_order_status(self, client_order_id: str) -> OrderResult: ...
    def cancel_order(self, client_order_id: str) -> OrderResult: ...
