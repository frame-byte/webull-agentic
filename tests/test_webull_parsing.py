"""Pins the Webull OpenAPI response shapes observed on a live account (values anonymized)."""
from app.brokers.webull_openapi import WebullOpenApiBroker


class Res:
    def __init__(self, body, code=200):
        self.body, self.status_code = body, code

    def json(self):
        return self.body


SNAPSHOT = [{"symbol": "AAPL", "price": "354.7830", "close": "354.8800", "ask": "355.0300", "bid": "354.7800",
             "extend_hour_last_price": "354.7830", "trade_status": "OVN"}]
BALANCE = {"total_asset_currency": "USD", "total_net_liquidation_value": "2438.01", "total_market_value": "0.00",
           "total_cash_balance": "2438.01",
           "account_currency_assets": [{"currency": "USD", "net_liquidation_value": "2438.01"}]}


def broker(snapshot=SNAPSHOT, balance=BALANCE, positions=()):
    b = WebullOpenApiBroker.__new__(WebullOpenApiBroker)  # skip SDK login
    b.account_id = "ACC"
    b.data = type("D", (), {"market_data": type("M", (), {
        "get_snapshot": staticmethod(lambda *a, **k: Res(snapshot))})()})()
    b.trade = type("T", (), {"account_v2": type("A", (), {
        "get_account_balance": staticmethod(lambda acc: Res(balance)),
        "get_account_position": staticmethod(lambda acc: Res(list(positions)))})()})()
    return b


def test_snapshot_parsing():
    q = broker().get_quote("AAPL")
    assert (q.last, q.bid, q.ask) == (354.783, 354.78, 355.03)


def test_balance_parsing():
    assert broker().get_equity() == 2438.01


def test_empty_positions():
    assert broker().get_position_qty("AAPL") == 0.0


def test_api_error_raises():
    b = broker()
    b.data.market_data.get_snapshot = lambda *a, **k: Res({"msg": "denied"}, 401)
    try:
        b.get_quote("AAPL")
    except RuntimeError as e:
        assert "401" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
