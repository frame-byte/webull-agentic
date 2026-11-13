"""The TradingView webhook payload and helpers to interpret it."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _num(v):
    """TradingView placeholders arrive as strings; blank/NaN -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "na", "null"):
        return None
    return float(s)


class Signal(BaseModel):
    secret: str
    strategy_id: str
    ticker: str
    exchange: Optional[str] = None
    action: Optional[str] = None            # buy / sell  ({{strategy.order.action}})
    market_position: Optional[str] = None   # long / flat / short ({{strategy.market_position}})
    position_size: Optional[float] = None   # signed ({{strategy.position_size}})
    contracts: Optional[float] = None
    price: Optional[float] = None
    bar_time: Optional[str] = None
    fired_at: Optional[str] = None
    order_id: Optional[str] = None

    @field_validator("position_size", "contracts", "price", mode="before")
    @classmethod
    def _nums(cls, v):
        return _num(v)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if ":" in v:  # "NASDAQ:AAPL"
            v = v.split(":", 1)[1]
        if not v or len(v) > 10:
            raise ValueError("bad ticker")
        return v

    @field_validator("action", "market_position")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) and v.strip() else None

    def idempotency_key(self) -> str:
        parts = [self.strategy_id, self.ticker, self.bar_time or "", self.order_id or "", self.action or "",
                 self.market_position or ""]
        if not (self.bar_time or self.order_id):
            parts.append(self.fired_at or "")
        return "|".join(parts)

    def fired_at_dt(self) -> Optional[datetime]:
        if not self.fired_at:
            return None
        try:
            dt = datetime.fromisoformat(self.fired_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def direction(self) -> Optional[str]:
        """Desired state: 'long', 'short' or 'flat'."""
        if self.market_position in ("long", "short", "flat"):
            return self.market_position
        if self.position_size is not None:
            return "long" if self.position_size > 0 else "short" if self.position_size < 0 else "flat"
        # Plain (non-strategy) alerts: buy => go long, sell => go flat.
        if self.action == "buy":
            return "long"
        if self.action == "sell":
            return "flat"
        return None


class Decision(BaseModel):
    accepted: bool
    reason: str
    target_qty: float = 0
    current_qty: float = 0
    delta_qty: float = 0
    side: Optional[str] = None
    limit_price: Optional[float] = None
    meta: dict = Field(default_factory=dict)
