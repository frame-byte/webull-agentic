"""Pure risk/sizing functions — no I/O, so every rule is unit-testable."""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import StrategyConfig

ET = ZoneInfo("America/New_York")


def market_session(now: datetime) -> str:
    """'core', 'extended' or 'closed' (US equities; exchange holidays are left to the broker to reject)."""
    t = now.astimezone(ET)
    if t.weekday() >= 5:
        return "closed"
    if time(9, 30) <= t.time() < time(16, 0):
        return "core"
    if time(4, 0) <= t.time() < time(20, 0):
        return "extended"
    return "closed"


def check_session(now: datetime, strat: StrategyConfig) -> Optional[str]:
    s = market_session(now)
    if s == "core" or (s == "extended" and strat.extended_hours):
        return None
    return f"market session is {s}"


def check_staleness(fired_at: Optional[datetime], now: datetime, max_age_s: int) -> Optional[str]:
    if fired_at is None:
        return None  # message template without {{timenow}}; allowed but discouraged
    age = (now - fired_at).total_seconds()
    if age > max_age_s:
        return f"signal is stale ({age:.0f}s old > {max_age_s}s)"
    if age < -30:
        return f"signal timestamp is {-age:.0f}s in the future"
    return None


def check_price(signal_price: Optional[float], live: float, max_dev_pct: float) -> Optional[str]:
    if signal_price is None or live <= 0:
        return None
    dev = abs(signal_price - live) / live * 100
    if dev > max_dev_pct:
        return f"signal price {signal_price} deviates {dev:.2f}% from live {live}"
    return None


def target_quantity(direction: str, strat: StrategyConfig, price: float,
                    tv_position_size: Optional[float], equity: Optional[float] = None) -> tuple[float, list[str]]:
    """Whole-share target position (signed) after sizing and caps. Returns (qty, notes)."""
    notes: list[str] = []
    if direction == "flat":
        return 0.0, notes
    if direction == "short" and not strat.allow_short:
        notes.append("short not allowed -> flat")
        return 0.0, notes

    if strat.sizing == "fixed_shares":
        qty = strat.shares
    elif strat.sizing == "fixed_dollars":
        qty = math.floor(strat.dollars / price) if price > 0 else 0
    elif strat.sizing == "percent_equity":
        # Share of account net liquidation value (cash, not margin buying power). Priced 1% above
        # last so the marketable-limit band and small moves still fit inside the cash.
        if not equity or price <= 0:
            notes.append("no equity reading -> 0 shares")
            qty = 0
        else:
            qty = math.floor(equity * strat.percent / 100 / (price * 1.01))
            notes.append(f"{strat.percent:g}% of equity ${equity:,.2f}")
    else:  # mirror
        qty = abs(tv_position_size or 0) * strat.multiplier
    qty = float(math.floor(qty))

    if qty > strat.max_shares:
        notes.append(f"capped by max_shares {strat.max_shares}")
        qty = float(math.floor(strat.max_shares))
    if price > 0 and qty * price > strat.max_notional:
        qty = float(math.floor(strat.max_notional / price))
        notes.append(f"capped by max_notional {strat.max_notional}")

    return (qty if direction == "long" else -qty), notes


def limit_price_for(side: str, live: float, bid: Optional[float], ask: Optional[float], band_pct: float) -> float:
    """Marketable limit: pay up to `band_pct` through the touch, never unbounded."""
    if side == "BUY":
        ref = ask if ask and ask > 0 else live
        px = ref * (1 + band_pct / 100)
    else:
        ref = bid if bid and bid > 0 else live
        px = ref * (1 - band_pct / 100)
    return round(px, 2) if px >= 1 else round(px, 4)


def check_daily_loss(start_equity: Optional[float], equity: Optional[float], max_loss_pct: float) -> Optional[str]:
    if not start_equity or equity is None:
        return None
    dd = (start_equity - equity) / start_equity * 100
    if dd >= max_loss_pct:
        return f"daily loss {dd:.2f}% >= limit {max_loss_pct}%"
    return None


def today_et(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(ET).strftime("%Y-%m-%d")


def start_of_day_ts(now: Optional[datetime] = None) -> float:
    t = (now or datetime.now(timezone.utc)).astimezone(ET)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
