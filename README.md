# TradingView → Webull auto-trader

When a TradingView alert fires, this gateway receives the webhook, runs it through risk
checks, and places an order on your Webull account through the **Webull OpenAPI**. The
order goes in directly, with no confirmation in the app.
Design rationale: [docs/adr/0001](docs/adr/0001-tradingview-to-webull-auto-trading.md).

```
TradingView alert ──HTTPS POST──► /webhook ─► checks (IP, secret, schema, duplicate) ─► 200 OK
                                       └─► background: risk engine ─► marketable LIMIT order ─► Webull
                                           reconciler (every 30s): fills / cancels stale orders
```

---

## Step 1: Run it locally and run the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## Step 2: Configure secrets and strategies

```bash
cp .env.example .env
cp config/strategies.example.yaml config/strategies.yaml
openssl rand -hex 24      # paste as WEBHOOK_SECRET in .env
```

- `.env`: set `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, and `WEBHOOK_SECRET`. Leave `MODE=dryrun`.
- `config/strategies.yaml`: one entry per TradingView strategy. Set the `symbols` it may
  trade, the sizing, and the caps. **Start with `shares: 1`.**

`.env` and `config/strategies.yaml` are git-ignored. Never commit them.

## Step 3: Check the Webull connection (read-only, no orders)

```bash
set -a; . ./.env; set +a
python -m scripts.check_webull AAPL
```

1. The first run prints your accounts. Copy the right `account_id` into `WEBULL_ACCOUNT_ID`.
2. Run it again. It prints balance, positions, and a quote, followed by the values the
   gateway parsed from them.
3. Make sure **quote, position qty, and equity are correct, not `None`**. Response fields
   are parsed defensively, but verify them before going live.
4. The SDK may ask for 2FA on first use. Approve it in the Webull app. The token is saved
   under `WEBULL_OPENAPI_TOKEN_DIR`, so keep that on persistent storage.

## Step 4: Deploy (DigitalOcean + Cloudflare)

`deploy/provision.py` creates, through the APIs:
- an Ubuntu 24.04 droplet (`s-1vcpu-1gb`, nyc3) booted by [`deploy/cloud-init.yaml`](deploy/cloud-init.yaml)
  (Docker, ufw, fail2ban, unattended upgrades, the repo, the gateway in **dryrun**)
- a cloud firewall: SSH 22, HTTP 80 (needed for the certificate), and **443 only from TradingView's IPs**
- a **DNS-only** (grey cloud) Cloudflare A record `trade.mdtrade.app`

```bash
DO_TOKEN=... CF_TOKEN=... python3 deploy/provision.py --ssh-key-name <your-key>           # plan only
DO_TOKEN=... CF_TOKEN=... python3 deploy/provision.py --ssh-key-name <your-key> --apply
```

Because 443 only accepts TradingView, reach the admin endpoints over SSH:

```bash
ssh ubuntu@trade.mdtrade.app 'curl -s localhost:8000/health'
```

### Where secrets live

| Secret | Where it goes | Never |
|---|---|---|
| Webull app key, secret, account ID | `/opt/gateway/.env` on the droplet (mode 600, owner `ubuntu`), added over SSH | the repo, cloud-init/user-data, chat, GitHub |
| Webhook secret | Generated on the droplet at boot into the same `.env`; copy it into the TradingView alert message | the repo |
| Webull 2FA token | `/opt/gateway/data/webull-token` (SDK writes it) | the repo |
| DigitalOcean and Cloudflare API tokens | Claude environment network secrets (injected by the proxy), or a password manager | the droplet, the repo |
| GitHub | Not needed: the repo is public and the droplet clones read-only over HTTPS. If you make it private, add a read-only **deploy key** generated on the droplet. If you add a GitHub Actions deploy later, use repo **Actions secrets**. | a personal access token on the droplet |

Add the Webull keys:

```bash
ssh ubuntu@trade.mdtrade.app
nano /opt/gateway/.env        # fill WEBULL_APP_KEY / WEBULL_APP_SECRET / WEBULL_ACCOUNT_ID
cd /opt/gateway && docker compose up -d
docker compose exec gateway python -m scripts.check_webull AAPL
```

## Step 5: Create the TradingView alerts

Do this on **tradingview.com**: webhooks require 2FA and can't be created through the
TradingView API connector.

1. Strategy settings: turn off "Recalculate on every tick" so signals don't repaint.
2. Create Alert, then set:
   - **Condition:** your strategy, "Order fills only".
   - **Message:** paste [`tradingview/alert_message.json`](tradingview/alert_message.json).
     Put in your secret and set `strategy_id` to match `strategies.yaml`.
   - **Notifications:** Webhook URL `https://trade.mdtrade.app/webhook`.
3. For indicator alerts (not strategies), use
   [`tradingview/indicator_alert_message.json`](tradingview/indicator_alert_message.json),
   with one alert for `"action": "buy"` and one for `"action": "sell"`. Set them to
   "Once Per Bar Close".

The gateway trades toward the strategy's **target position** (`market_position`), not
"buy X more". A repeated alert is therefore a no-op, and a missed alert gets corrected by
the next one.

## Step 6: Dry run (1–2 weeks)

With `MODE=dryrun`, every signal goes through the full pipeline using **live Webull
quotes**, but fills are simulated. Check the journal daily against TradingView's
strategy tester:

```bash
ssh ubuntu@trade.mdtrade.app 'cd /opt/gateway && . ./.env && curl -s -H "x-admin-token: $WEBHOOK_SECRET" localhost:8000/admin/journal'
```

## Step 7: Go live, small

1. Set `MODE=live` in `.env`, keep `shares: 1`, then run `docker compose up -d`.
1b. `cd /opt/gateway && sed -i s/^MODE=.*/MODE=live/ .env && docker compose up -d`
2. Watch the first live fills in the Webull app.
3. Raise sizing in `strategies.yaml` once fills match expectations.

## Operating it

| Action | Command |
|---|---|
| **Stop all trading now** | `ssh ubuntu@trade.mdtrade.app 'cd /opt/gateway && . ./.env && curl -s -X POST -H "x-admin-token: $WEBHOOK_SECRET" "localhost:8000/admin/kill?on=true"'` |
| Resume | same command with `on=false` |
| Health | `ssh ubuntu@trade.mdtrade.app 'curl -s localhost:8000/health'` |
| Logs | `ssh ubuntu@trade.mdtrade.app 'cd /opt/gateway && docker compose logs -f gateway'` |

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to get a phone message for every order,
reject, fill, and error.

### Risk rules (any failure rejects the signal and notifies you)

| Rule | Setting |
|---|---|
| Strategy/symbol allowlist and per-strategy `enabled` | `strategies.yaml` |
| Global kill switch | `/admin/kill` |
| Regular hours only, unless `extended_hours: true` | per strategy |
| Signal older than N seconds | `MAX_SIGNAL_AGE_S` (60) |
| Signal price vs live quote deviation | `MAX_PRICE_DEVIATION_PCT` (1.5) |
| Max shares and max $ per symbol | `max_shares`, `max_notional` |
| Orders per day | `MAX_ORDERS_PER_DAY` (20) |
| Daily equity drawdown trips the kill switch | `MAX_DAILY_LOSS_PCT` (3) |
| Long-only unless `allow_short: true`; long→short goes flat first | per strategy |
| Orders are marketable LIMIT, at most N% through bid/ask | `LIMIT_BAND_PCT` (0.3) |
| Unfilled orders cancelled after N seconds | `ORDER_TIMEOUT_S` (120) |

### Known limits

- Exchange holidays aren't modeled; Webull rejects those orders and you're notified.
- If you trade the same symbols by hand, the gateway counts those shares when it
  computes the target. Use symbols the bot owns exclusively.
- Pattern-day-trader rules apply if the account is under $25k.
