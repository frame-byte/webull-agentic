from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import risk
from app.brokers.dryrun import DryRunBroker
from app.config import Settings, StrategyConfig
from app.executor import Executor
from app.main import create_app
from app.models import Signal
from app.notify import Notifier
from app.store import Store

SECRET = "s" * 24
# Wednesday 2026-11-11 11:00 ET (16:00 UTC) — regular session.
NOW = datetime(2026, 11, 11, 16, 0, tzinfo=timezone.utc)


def make(prices=None, **strat_kw):
    strat = StrategyConfig(strategy_id="ema", symbols=["AAPL", "TSLA"], **strat_kw)
    s = Settings(webhook_secret=SECRET, db_path=":memory:", strategies={"ema": strat})
    broker = DryRunBroker(prices=prices or {"AAPL": 200.0, "TSLA": 300.0})
    ex = Executor(s, Store(":memory:"), broker, Notifier())
    return s, ex, broker


def sig(**kw):
    base = dict(secret=SECRET, strategy_id="ema", ticker="NASDAQ:AAPL", market_position="long",
                price="200", bar_time="2026-11-11T15:55:00Z", fired_at=NOW.isoformat(), order_id="L")
    base.update(kw)
    return Signal.model_validate(base)


def run(ex, s, now=NOW):
    sid = ex.store.insert_signal(s.idempotency_key(), s.strategy_id, s.ticker, s.model_dump())
    return ex.process(sid, s, now=now)


# ---------- signal parsing ----------
def test_ticker_and_direction_parsing():
    s = sig(market_position=None, position_size="-5", action="sell")
    assert s.ticker == "AAPL" and s.direction() == "short"
    assert sig(market_position=None, action="buy").direction() == "long"
    assert sig(market_position=None, action="sell").direction() == "flat"
    assert sig(price="NaN").price is None


# ---------- risk rules ----------
def test_session():
    strat = StrategyConfig("x", ["A"])
    assert risk.check_session(NOW, strat) is None
    pre = NOW.replace(hour=12)  # 7:00 ET
    assert "extended" in risk.check_session(pre, strat)
    assert risk.check_session(pre, StrategyConfig("x", ["A"], extended_hours=True)) is None
    assert "closed" in risk.check_session(datetime(2026, 11, 14, 16, tzinfo=timezone.utc), strat)  # Saturday


def test_sizing_and_caps():
    st = StrategyConfig("x", ["A"], sizing="fixed_dollars", dollars=1000, max_notional=500)
    q, notes = risk.target_quantity("long", st, 99.0, None)
    assert q == 5 and notes  # 1000/99=10 -> capped to 500/99=5
    assert risk.target_quantity("short", StrategyConfig("x", ["A"]), 10, None)[0] == 0
    assert risk.target_quantity("short", StrategyConfig("x", ["A"], allow_short=True, shares=3), 10, None)[0] == -3
    assert risk.target_quantity("long", StrategyConfig("x", ["A"], sizing="mirror", multiplier=0.5), 10, 7)[0] == 3


def test_limit_price_is_bounded():
    assert risk.limit_price_for("BUY", 100, 99.9, 100.1, 0.3) == 100.4
    assert risk.limit_price_for("SELL", 100, 99.9, 100.1, 0.3) == 99.6


# ---------- executor ----------
def test_buy_then_flat_then_idempotent_target():
    s, ex, b = make(shares=10)
    d = run(ex, sig())
    assert d.accepted and d.side == "BUY" and d.delta_qty == 10 and b.positions["AAPL"] == 10
    d = run(ex, sig(order_id="L2"))           # same target again -> no order
    assert d.accepted and d.delta_qty == 0 and len(b.orders) == 1
    d = run(ex, sig(market_position="flat", order_id="X"))
    assert d.side == "SELL" and d.delta_qty == -10 and b.positions["AAPL"] == 0


def test_long_to_short_flattens_first():
    s, ex, b = make(shares=5, allow_short=True)
    run(ex, sig())
    d = run(ex, sig(market_position="short", order_id="S"))
    assert d.delta_qty == -5 and b.positions["AAPL"] == 0


@pytest.mark.parametrize("kw,reason", [
    (dict(ticker="MSFT"), "not allowed"),
    (dict(strategy_id="nope"), "unknown strategy"),
    (dict(fired_at=(NOW - timedelta(minutes=5)).isoformat()), "stale"),
    (dict(price="210"), "deviates"),
    (dict(market_position=None, action=None), "direction"),
])
def test_rejections(kw, reason):
    s, ex, b = make()
    d = run(ex, sig(**kw))
    assert not d.accepted and reason in d.reason and not b.orders


def test_kill_switch_and_daily_loss():
    s, ex, b = make()
    ex.set_kill(True)
    assert "kill" in run(ex, sig()).reason
    ex.set_kill(False)
    run(ex, sig())                             # records start equity 100k
    b.equity = 96_000                          # -4% > 3% limit
    d = run(ex, sig(market_position="flat", order_id="F"))
    assert not d.accepted and "daily loss" in d.reason and ex.killed()


def test_max_orders_per_day():
    s, ex, b = make()
    s.max_orders_per_day = 2
    run(ex, sig(order_id="1"))
    run(ex, sig(market_position="flat", order_id="2"))
    d = run(ex, sig(order_id="3"))
    assert not d.accepted and "max_orders_per_day" in d.reason


# ---------- HTTP ----------
def client(**settings_kw):
    s, ex, b = make()
    for k, v in settings_kw.items():
        setattr(s, k, v)
    app = create_app(s, ex, reconcile_interval_s=0)
    return TestClient(app), ex, b


def test_webhook_auth_and_dedupe(monkeypatch):
    monkeypatch.setattr("app.executor.datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    c, ex, b = client(enforce_ip_allowlist=False)
    body = sig().model_dump()
    assert c.post("/webhook", json={**body, "secret": "wrong" * 5}).status_code == 401
    assert c.post("/webhook", content="not json").status_code == 400
    r = c.post("/webhook", json=body)
    assert r.json()["status"] == "accepted"
    assert c.post("/webhook", json=body).json()["status"] == "duplicate"
    assert b.positions.get("AAPL") == 1


def test_webhook_ip_allowlist():
    c, ex, b = client()  # TestClient host is "testclient", not a TradingView IP
    assert c.post("/webhook", json=sig().model_dump()).status_code == 403


def test_admin_endpoints():
    c, ex, b = client()
    assert c.post("/admin/kill").status_code == 401
    assert c.post("/admin/kill?on=true", headers={"x-admin-token": SECRET}).json()["kill_switch"] is True
    assert c.get("/health").json()["kill_switch"] is True


def test_reconcile_fill_and_timeout():
    from app.brokers.base import OrderResult
    s, ex, b = make()
    statuses = {"a": "filled", "b": "submitted"}
    b.get_order_status = lambda coid: OrderResult(True, coid, statuses[coid])
    ex.store.insert_order("a", 1, "AAPL", "BUY", 1, 200, "submitted", {})
    ex.store.insert_order("b", 2, "TSLA", "BUY", 1, 300, "submitted", {})
    import time
    ex.reconcile(time.time() + s.order_timeout_s + 1)
    by = {o["client_order_id"]: o["status"] for o in ex.store.recent_orders()}
    assert by == {"a": "filled", "b": "cancelled"}


def test_webhook_tolerates_linebreak_in_secret(monkeypatch):
    # Reproduces a real TradingView alert: the pasted secret carried a newline before its closing quote.
    monkeypatch.setattr("app.executor.datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    c, ex, b = client(enforce_ip_allowlist=False)
    body = ('{\n  "secret": "' + SECRET + '\n",\n  "strategy_id": "ema",\n  "ticker": "AAPL",\n'
            '  "action": "buy",\n  "market_position": "long",\n  "price": "200",\n'
            f'  "bar_time": "2026-11-11T15:00:00Z",\n  "fired_at": "{NOW.isoformat()}",\n  "order_id": "Long"\n}}\n')
    r = c.post("/webhook", content=body, headers={"content-type": "text/plain; charset=utf-8"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    # Truly malformed JSON still gets a 400 with the reason.
    r = c.post("/webhook", content='{"secret": "x",}')
    assert r.status_code == 400 and "JSON" in r.json()["detail"]


def test_dryrun_equity_does_not_trip_live_loss_limit():
    s, ex, b = make()
    run(ex, sig(order_id="1"))                 # dry run records simulated 100k for today
    b.name, b.equity = "webull_openapi", 2_438  # same day, now "live" with real equity
    d = run(ex, sig(market_position="flat", order_id="2"))
    assert d.accepted and not ex.killed()


def test_filled_order_that_cannot_be_cancelled_does_not_block():
    from app.brokers.base import OrderResult
    s, ex, b = make()
    ex.store.insert_order("old", 1, "AAPL", "BUY", 1, 200, "submitted", {})  # filled at broker, stale here
    b.cancel_order = lambda coid: OrderResult(False, coid, "unknown", error="order already filled")
    b.get_order_status = lambda coid: OrderResult(True, coid, "filled")
    d = run(ex, sig())
    assert d.accepted and d.side == "BUY"
    assert {o["client_order_id"]: o["status"] for o in ex.store.recent_orders()}["old"] == "filled"
