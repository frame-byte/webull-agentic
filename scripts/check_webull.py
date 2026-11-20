"""Read-only Webull OpenAPI check. Places NO orders.

  python -m scripts.check_webull AAPL
  python -m scripts.check_webull SOXL --preview-stop 8

Prints accounts, balance, positions and a quote, then shows what the gateway parsed from them.
--preview-stop PCT asks Webull to validate (preview) a trailing-stop SELL for your position
(or 1 share if you hold none). Preview never places an order.
"""
import json
import os
import sys

from app.brokers.webull_openapi import WebullOpenApiBroker


def show(title, res):
    print(f"\n== {title} (HTTP {getattr(res, 'status_code', '?')})")
    try:
        print(json.dumps(res.json(), indent=2)[:3000])
    except Exception:
        print(getattr(res, "text", res))


def main():
    args = [a for a in sys.argv[1:]]
    pct = None
    if "--preview-stop" in args:
        i = args.index("--preview-stop")
        pct = float(args[i + 1])
        del args[i:i + 2]
    symbol = (args[0] if args else "AAPL").upper()
    b = WebullOpenApiBroker(os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"],
                            os.environ.get("WEBULL_ACCOUNT_ID", ""), os.environ.get("WEBULL_REGION", "us"))
    show("account list", b.trade.account_v2.get_account_list())
    if not b.account_id:
        print("\nSet WEBULL_ACCOUNT_ID to one of the account_id values above and rerun.")
        return
    show("balance", b.trade.account_v2.get_account_balance(b.account_id))
    show("positions", b.trade.account_v2.get_account_position(b.account_id))
    show(f"snapshot {symbol}", b.data.market_data.get_snapshot(symbol, "US_STOCK", extend_hour_required=True))

    print("\n== parsed by gateway")
    print("quote:", b.get_quote(symbol))
    print("position qty:", b.get_position_qty(symbol))
    print("equity:", b.get_equity())
    print("\nIf quote/position/equity look wrong or None, send me the raw output above (no keys in it).")

    if pct is not None:
        import uuid
        qty = b.get_position_qty(symbol) or 1
        order = b.trailing_stop_order(uuid.uuid4().hex, symbol, qty, pct)
        print(f"\n== PREVIEW ONLY (no order placed): trailing stop {pct:g}% SELL {qty:g} {symbol}")
        print(json.dumps(order, indent=2))
        show("preview response", b.trade.order_v2.preview_order(b.account_id, [order]))
        print("\nHTTP 200 above = Webull accepts this trailing-stop format.")


if __name__ == "__main__":
    main()
