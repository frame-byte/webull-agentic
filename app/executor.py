"""Turns an accepted webhook signal into (at most) one broker order."""
from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from . import risk
from .brokers.base import Broker
from .config import Settings
from .models import Decision, Signal
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)

KILL_KEY = "kill_switch"


class Executor:
    def __init__(self, settings: Settings, store: Store, broker: Broker, notifier: Notifier):
        self.s = settings
        self.store = store
        self.broker = broker
        self.notifier = notifier
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    # -- kill switch --
    def killed(self) -> bool:
        return self.store.get(KILL_KEY, "off") == "on"

    def set_kill(self, on: bool, reason: str = "") -> None:
        self.store.set(KILL_KEY, "on" if on else "off")
        self.notifier.send(f"Kill switch {'ON' if on else 'OFF'}{': ' + reason if reason else ''}")

    # -- main entry --
    def process(self, signal_id: int, sig: Signal, now: Optional[datetime] = None) -> Decision:
        now = now or datetime.now(timezone.utc)
        with self._locks[sig.ticker]:
            try:
                d = self._decide_and_execute(signal_id, sig, now)
            except Exception as e:
                log.exception("signal %s failed", signal_id)
                self.store.set_signal_status(signal_id, "error", str(e))
                self.notifier.send(f"ERROR {sig.strategy_id} {sig.ticker}: {e}")
                return Decision(accepted=False, reason=f"error: {e}")
        if not d.accepted:
            self.store.set_signal_status(signal_id, "rejected", d.reason)
            self.notifier.send(f"REJECTED {sig.strategy_id} {sig.ticker}: {d.reason}")
        elif d.delta_qty == 0:
            self.store.set_signal_status(signal_id, "noop", d.reason)
        return d

    def _reject(self, reason: str, **kw) -> Decision:
        return Decision(accepted=False, reason=reason, **kw)

    def _decide_and_execute(self, signal_id: int, sig: Signal, now: datetime) -> Decision:
        s = self.s
        strat = s.strategies.get(sig.strategy_id)
        if strat is None:
            return self._reject(f"unknown strategy {sig.strategy_id!r}")
        if sig.ticker not in strat.symbols:
            return self._reject(f"{sig.ticker} not allowed for {sig.strategy_id}")
        if self.killed():
            return self._reject("kill switch is on")
        if not strat.enabled:
            return self._reject("strategy disabled")
        if r := risk.check_session(now, strat):
            return self._reject(r)
        if r := risk.check_staleness(sig.fired_at_dt(), now, s.max_signal_age_s):
            return self._reject(r)
        direction = sig.direction()
        if direction is None:
            return self._reject("cannot determine direction (need market_position, position_size or action)")

        quote = self.broker.get_quote(sig.ticker)
        if r := risk.check_price(sig.price, quote.last, s.max_price_deviation_pct):
            return self._reject(r)

        # Daily loss limit (equity vs first reading of the day).
        equity = self.broker.get_equity()
        day_key = f"start_equity:{risk.today_et(now)}"
        start_eq = self.store.get(day_key)
        if start_eq is None and equity is not None:
            self.store.set(day_key, str(equity))
            start_eq = str(equity)
        if r := risk.check_daily_loss(float(start_eq) if start_eq else None, equity, s.max_daily_loss_pct):
            self.set_kill(True, r)
            return self._reject(r)

        # A newer signal supersedes any working order for this symbol.
        if prev := self.store.open_order_for(sig.ticker):
            res = self.broker.cancel_order(prev["client_order_id"])
            self.store.update_order(prev["client_order_id"], "cancelled" if res.ok else prev["status"], res.raw)
            if not res.ok:
                return self._reject(f"could not cancel working order {prev['client_order_id']}: {res.error}")

        current = self.broker.get_position_qty(sig.ticker)
        target, notes = risk.target_quantity(direction, strat, quote.last, sig.position_size)
        delta = target - current
        base = dict(target_qty=target, current_qty=current, delta_qty=delta, meta={"notes": notes})

        if delta == 0:
            return Decision(accepted=True, reason="already at target", **base)
        if current >= 0 > target or current <= 0 < target:
            # Crossing through zero: only flatten now; the next signal can open the other side.
            if current != 0:
                target, delta = 0.0, -current
                base.update(target_qty=0.0, delta_qty=delta)
                notes.append("crossing zero: flatten first")

        if self.store.orders_since(risk.start_of_day_ts(now)) >= s.max_orders_per_day:
            return self._reject(f"max_orders_per_day {s.max_orders_per_day} reached", **base)

        side = "BUY" if delta > 0 else "SELL"
        qty = abs(delta)
        limit = risk.limit_price_for(side, quote.last, quote.bid, quote.ask, s.limit_band_pct)
        coid = uuid.uuid4().hex
        res = self.broker.place_limit_order(coid, sig.ticker, side, qty, limit, strat.extended_hours)
        self.store.insert_order(coid, signal_id, sig.ticker, side, qty, limit, res.status, res.raw or res.error)

        if not res.ok:
            return self._reject(f"broker rejected order: {res.error}", side=side, limit_price=limit, **base)

        self.store.set_signal_status(signal_id, "ordered", "; ".join(notes))
        self.notifier.send(f"{'[DRYRUN] ' if res.status == 'dryrun' else ''}{side} {qty:g} {sig.ticker} "
                           f"@ LMT {limit} ({sig.strategy_id}; {current:g} -> {target:g})")
        return Decision(accepted=True, reason="order placed", side=side, limit_price=limit,
                        **base | {"meta": {"notes": notes, "client_order_id": coid}})

    # -- reconciliation (called periodically) --
    def reconcile(self, now_ts: float) -> None:
        for o in self.store.open_orders():
            res = self.broker.get_order_status(o["client_order_id"])
            if res.ok and res.status not in ("unknown", o["status"]):
                self.store.update_order(o["client_order_id"], res.status, res.raw)
                if res.status in ("filled", "rejected", "cancelled"):
                    self.notifier.send(f"Order {o['side']} {o['qty']:g} {o['symbol']} -> {res.status.upper()}")
                if res.status != "partial":
                    continue
            if now_ts - o["created_at"] > self.s.order_timeout_s:
                c = self.broker.cancel_order(o["client_order_id"])
                if c.ok:
                    self.store.update_order(o["client_order_id"], "cancelled", c.raw)
                    self.notifier.send(f"Cancelled stale order {o['side']} {o['qty']:g} {o['symbol']} "
                                       f"after {self.s.order_timeout_s}s unfilled")
