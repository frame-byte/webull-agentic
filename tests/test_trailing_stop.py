import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.brokers.base import OrderResult
from app.brokers.webull_openapi import WebullOpenApiBroker
from app.config import load_strategies
from app.store import Store
from tests.test_gateway import NOW, make, run, sig

T = NOW.timestamp()  # Wed 11:00 ET, inside the 9:00-15:58 ET stop window


def stops(ex, status=None):
    rows = [o for o in ex.store.recent_orders(200) if o["kind"] == "trailing_stop"]
    return [o for o in rows if status is None or o["status"] == status]


def setup(**kw):
    s, ex, b = make(shares=27, trailing_stop_pct=8, max_notional=1e9, **kw)
    return s, ex, b


def test_stop_placed_on_long_position_once():
    s, ex, b = setup()
    run(ex, sig())                                   # buy 27
    ex.reconcile(T)
    (st,) = stops(ex, "submitted")
    assert (st["symbol"], st["side"], st["qty"]) == ("AAPL", "SELL", 27)
    assert b.stops[st["client_order_id"]]["trail_pct"] == 8
    assert any("Trailing stop 8% placed on 27 AAPL" in m for m in ex.notifier.sent)
    ex.reconcile(T + 30)
    assert len(stops(ex)) == 1                       # no duplicate


def test_no_stop_without_position_or_outside_window():
    s, ex, b = setup()
    ex.reconcile(T)
    assert stops(ex) == []                           # flat: nothing to protect
    run(ex, sig())
    early = datetime(2026, 11, 11, 13, 30, tzinfo=timezone.utc).timestamp()   # 8:30 ET
    saturday = datetime(2026, 11, 14, 16, 0, tzinfo=timezone.utc).timestamp()
    close = datetime(2026, 11, 11, 20, 59, tzinfo=timezone.utc).timestamp()   # 15:59 ET
    for t in (early, saturday, close):
        ex.reconcile(t)
    assert stops(ex) == []


def test_exit_signal_cancels_stop_before_selling():
    s, ex, b = setup()
    run(ex, sig())
    ex.reconcile(T)
    (st,) = stops(ex)
    d = run(ex, sig(market_position="flat", order_id="X"))
    assert d.accepted and d.side == "SELL" and d.delta_qty == -27
    assert b.stops[st["client_order_id"]]["status"] == "cancelled"
    assert b.positions["AAPL"] == 0                  # sold exactly once
    ex.reconcile(T + 60)
    assert stops(ex, "submitted") == []              # flat: no new stop


def test_stop_hit_then_strategy_exit_is_noop():
    s, ex, b = setup()
    run(ex, sig())
    ex.reconcile(T)
    (st,) = stops(ex)
    b.trigger_stop(st["client_order_id"])            # Webull sells on the trailing stop
    ex.reconcile(T + 30)
    assert stops(ex)[0]["status"] == "filled"
    assert any("Trailing stop SOLD 27 AAPL" in m for m in ex.notifier.sent)
    d = run(ex, sig(market_position="flat", order_id="X"))
    assert d.accepted and d.delta_qty == 0 and b.positions["AAPL"] == 0


def test_stop_hit_between_reconciles_is_detected_on_exit_signal():
    s, ex, b = setup()
    run(ex, sig())
    ex.reconcile(T)
    (st,) = stops(ex)
    b.trigger_stop(st["client_order_id"])            # filled, but reconcile hasn't seen it yet
    d = run(ex, sig(market_position="flat", order_id="X"))
    assert d.accepted and d.delta_qty == 0           # cancel fails -> status filled -> nothing to sell
    assert stops(ex)[0]["status"] == "filled" and b.positions["AAPL"] == 0


def test_daily_replacement_of_day_only_stop():
    s, ex, b = setup()
    run(ex, sig())
    ex.reconcile(T)
    (old,) = stops(ex)
    next_day = T + 24 * 3600                         # Thursday 11:00 ET
    ex.reconcile(next_day)
    assert {o["client_order_id"]: o["status"] for o in stops(ex)}[old["client_order_id"]] == "cancelled"
    assert len(stops(ex, "submitted")) == 1


def test_stop_resized_when_position_changes_and_dropped_when_sold_by_hand():
    s, ex, b = setup()
    run(ex, sig())
    ex.reconcile(T)
    b.positions["AAPL"] = 30                         # bought 3 more by hand
    ex.reconcile(T + 30)
    live = stops(ex, "submitted")
    assert len(live) == 1 and live[0]["qty"] == 30
    b.positions["AAPL"] = 0                          # sold everything by hand
    ex.reconcile(T + 60)
    assert stops(ex, "submitted") == []


def test_waits_while_entry_order_is_working():
    s, ex, b = setup()
    b.positions["AAPL"] = 10
    ex.store.insert_order("entry1", 1, "AAPL", "BUY", 17, 200, "submitted", {}, created_at=T)
    b.get_order_status = lambda coid: OrderResult(True, coid, "submitted") if coid == "entry1" else \
        OrderResult(True, coid, b.stops[coid]["status"])
    ex.reconcile(T + 10)
    assert stops(ex) == []                           # partial position: wait for the entry to settle


def test_placement_failure_backs_off_and_alerts_once():
    s, ex, b = setup()
    run(ex, sig())
    b.place_trailing_stop = lambda *a: OrderResult(False, a[0], "rejected", error="market closed")
    ex.reconcile(T)
    ex.reconcile(T + 30)                             # inside 5-minute backoff: no retry
    ex.reconcile(T + 400)                            # retried, still failing
    assert len(stops(ex)) == 2
    assert sum("Could not place trailing stop" in m for m in ex.notifier.sent) == 1


def test_stops_do_not_count_toward_daily_order_limit():
    s, ex, b = setup()
    s.max_orders_per_day = 1
    run(ex, sig())
    ex.reconcile(T)
    ex.reconcile(T + 24 * 3600)
    assert ex.store.orders_since(T - 3600) == 1


def test_disabled_when_pct_zero():
    s, ex, b = make(shares=27)
    run(ex, sig())
    ex.reconcile(T)
    assert stops(ex) == []


def test_webull_trailing_order_matches_docs():
    o = WebullOpenApiBroker.trailing_stop_order("c1", "SOXL", 27, 8)
    assert o == {
        "client_order_id": "c1", "combo_type": "NORMAL", "symbol": "SOXL", "instrument_type": "EQUITY",
        "market": "US", "order_type": "TRAILING_STOP_LOSS", "trailing_type": "PERCENTAGE",
        "trailing_stop_step": "0.08", "quantity": "27", "side": "SELL", "time_in_force": "DAY",
        "support_trading_session": "CORE", "entrust_type": "QTY",
    }


def test_old_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE orders (client_order_id TEXT PRIMARY KEY, signal_id INTEGER,
        created_at REAL NOT NULL, symbol TEXT, side TEXT, qty REAL, limit_price REAL, status TEXT NOT NULL,
        broker_response TEXT, updated_at REAL);
        INSERT INTO orders VALUES ('a',1,1,'SOXL','BUY',27,41.97,'filled','{}',1);""")
    c.commit(); c.close()
    st = Store(str(db))
    assert st.recent_orders()[0]["kind"] == "entry"
    assert st.orders_since(0) == 1


@pytest.mark.parametrize("pct,ok", [(8, True), (0.5, True), (0.1, False), (80, False)])
def test_config_validation(tmp_path, pct, ok):
    f = tmp_path / "s.yaml"
    f.write_text(f"strategies:\n  x:\n    symbols: [SOXL]\n    trailing_stop_pct: {pct}\n")
    if ok:
        assert load_strategies(str(f))["x"].trailing_stop_pct == pct
    else:
        with pytest.raises(ValueError):
            load_strategies(str(f))
