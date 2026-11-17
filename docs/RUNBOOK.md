# Gateway runbook: everyday commands

These are copy-and-run commands for the TradingView → Webull gateway on
`trade.mdtrade.app`. Unless a section says otherwise, run them **on the server**
after logging in (section 1). Each command is in its own block so you can copy it on its own.

The admin commands read the webhook secret from `/opt/gateway/.env` at the moment you run
them, so they keep working after you change the secret.

---

## 1. Connect to the server

On your own computer:

```bash
ssh -i ~/.ssh/webull_gateway ubuntu@trade.mdtrade.app
```

All later commands start from the app folder:

```bash
cd /opt/gateway
```

---

## 2. Is it running?

**Quick health check.** Expect `"ok":true`, `"mode":"live"`, `"broker":"webull_openapi"`, `"kill_switch":false`.

```bash
curl -s localhost:8000/health
```

**Container status.** `gateway`, `watchdog` and `caddy` should all say `Up`. `Restarting` means the gateway is crashing; see section 9.

```bash
cd /opt/gateway && docker compose ps
```

---

## 3. Stop or resume trading (kill switch)

**Stop all new buys now.** Sells that close a position still go through.

```bash
cd /opt/gateway && . ./.env && curl -s -X POST -H "x-admin-token: $WEBHOOK_SECRET" "localhost:8000/admin/kill?on=true"
```

**Resume trading.** It should print `{"kill_switch":false}`.

```bash
cd /opt/gateway && . ./.env && curl -s -X POST -H "x-admin-token: $WEBHOOK_SECRET" "localhost:8000/admin/kill?on=false"
```

**Why did the kill switch turn on?** It shows `daily loss … >= limit` or `manual`.

```bash
cd /opt/gateway && docker compose logs gateway | grep -i "kill switch"
```

> If the daily loss limit tripped it **today**, the next signal turns it back on until
> tomorrow, unless you raise the limit first (section 6).

---

## 4. What has it done? (signals and orders)

**Last 20 signals and orders, formatted.** For each signal, `status` is `ordered`,
`rejected`, `noop` or `error`, and `reason` says why.

```bash
cd /opt/gateway && . ./.env && curl -s -H "x-admin-token: $WEBHOOK_SECRET" "localhost:8000/admin/journal?limit=20" | python3 -m json.tool
```

**One line per signal, newest first:**

```bash
cd /opt/gateway && . ./.env && curl -s -H "x-admin-token: $WEBHOOK_SECRET" "localhost:8000/admin/journal?limit=20" | python3 -c "import sys,json,datetime as d;[print(d.datetime.fromtimestamp(s['received_at']).strftime('%m-%d %H:%M'),s['ticker'],s['status'],s['reason'] or '') for s in json.load(sys.stdin)['signals']]"
```

**Orders, alerts and errors as they happen.** Press Ctrl+C to stop.

```bash
cd /opt/gateway && docker compose logs -f gateway | grep -E "NOTIFY|WARNING|ERROR"
```

**Full gateway log, last 100 lines:**

```bash
cd /opt/gateway && docker compose logs --tail 100 gateway
```

---

## 5. Check the Webull account (read-only, places no orders)

**Account value, positions, and a live quote.** Replace `SOXL` with any ticker. Check
the "parsed by gateway" lines at the end.

```bash
cd /opt/gateway && docker compose exec gateway python -m scripts.check_webull SOXL
```

---

## 6. Change settings

### Strategies and position size (`config/strategies.yaml`)

**1. Edit the file:**

```bash
nano /opt/gateway/config/strategies.yaml
```

**2. Always check that it loads before restarting.** It should print `live [...your strategy names...]`.
If it prints an error instead, fix the file. Restarting with a broken file takes the gateway down.

```bash
cd /opt/gateway && docker compose run --rm gateway python -c "from app.config import Settings; s=Settings.from_env(); print(s.mode, list(s.strategies))"
```

**3. Apply the change:**

```bash
cd /opt/gateway && docker compose up -d && sleep 5 && curl -s localhost:8000/health
```

The current known-good file looks like this. To restore it, run:

```bash
cat > /opt/gateway/config/strategies.yaml <<'EOF'
strategies:
  "Webull SOXL-RSI-EMA Bar Close Strategy":
    enabled: true
    symbols: [SOXL]
    sizing: percent_equity
    percent: 95
    extended_hours: true
    max_shares: 1000
    max_notional: 10000
EOF
```

Sizing options: `fixed_shares` with `shares: N`, `fixed_dollars` with `dollars: N`, or
`percent_equity` with `percent: 1–100`. `percent_equity` uses cash only, never margin.

### Risk limits (`.env`)

**Show the current limits:**

```bash
grep -E "^(MODE|MAX_|LIMIT_BAND|ORDER_TIMEOUT)" /opt/gateway/.env
```

**Change the daily loss limit,** for example to 8%:

```bash
cd /opt/gateway && sed -i 's/^MAX_DAILY_LOSS_PCT=.*/MAX_DAILY_LOSS_PCT=8/' .env && docker compose up -d
```

**Edit any setting by hand.** Run `docker compose up -d` afterwards; `restart` does not reload `.env`.

```bash
nano /opt/gateway/.env
```

| Setting | Meaning |
|---|---|
| `MAX_DAILY_LOSS_PCT` | Daily drop in account value that turns the kill switch on |
| `MAX_ORDERS_PER_DAY` | New-position orders per day (exits always allowed) |
| `MAX_SIGNAL_AGE_S` | Reject alerts older than this many seconds |
| `MAX_PRICE_DEVIATION_PCT` | Reject if the alert price is this far from the live price |
| `LIMIT_BAND_PCT` | How far past the bid/ask the limit order may go |
| `ORDER_TIMEOUT_S` | Cancel unfilled orders after this many seconds |

### Live or dry run

**Switch to dry run.** Real prices, simulated fills, no real orders.

```bash
cd /opt/gateway && sed -i 's/^MODE=.*/MODE=dryrun/' .env && docker compose up -d && sleep 5 && curl -s localhost:8000/health
```

**Switch back to live:**

```bash
cd /opt/gateway && sed -i 's/^MODE=.*/MODE=live/' .env && docker compose up -d && sleep 5 && curl -s localhost:8000/health
```

---

## 7. Change the webhook secret

Do this when no signal is about to fire. Webhooks fail with 401 until TradingView has the new value.

**1. Generate a new secret, restart, and show it:**

```bash
cd /opt/gateway && sed -i "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=$(openssl rand -hex 24)|" .env && docker compose up -d && grep WEBHOOK_SECRET .env
```

**2.** In TradingView, edit each alert that posts to `https://trade.mdtrade.app/webhook`
and replace the value, keeping it **on one line**:

```
"secret": "<new value>",
```

---

## 8. Telegram alerts

**Send a test message:**

```bash
cd /opt/gateway && docker compose run --rm watchdog python /watchdog.py --test
```

**Watchdog log.** Shows every alert it sent or tried to send.

```bash
cd /opt/gateway && docker compose logs --tail 50 watchdog
```

**Find your chat ID** (after messaging your bot once). Replace `<TOKEN>`.

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
```

---

## 9. Troubleshooting

### TradingView says "Webhook delivery failed"

| Code | Meaning | What to do |
|---|---|---|
| **502** | The gateway is down or restarting | Run the two commands below |
| **400** | The alert message isn't valid JSON | Check the message for a missing quote or comma, a trailing comma, or a line break inside a value |
| **401** | Wrong secret in the alert message | Copy it again from `grep WEBHOOK_SECRET /opt/gateway/.env`, on one line |
| **403** | The request didn't come from TradingView's servers | Expected for anything that isn't TradingView |
| **422** | The JSON is valid but a field is missing or wrong | Check `ticker` and `strategy_id` against the template |
| 200 but no trade | Received, then rejected | Check the journal's `reason` (section 4) |

**Why is the gateway down?** The error is in the last lines, usually a YAML mistake in `strategies.yaml`.

```bash
cd /opt/gateway && docker compose ps && docker compose logs --tail 40 gateway
```

**Check the config loads:**

```bash
cd /opt/gateway && docker compose run --rm gateway python -c "from app.config import Settings; s=Settings.from_env(); print(s.mode, list(s.strategies))"
```

### Common rejection reasons in the journal

| Reason | Meaning |
|---|---|
| `unknown strategy '…'` | The alert's `strategy_id` doesn't exactly match a name in `strategies.yaml` |
| `market session is closed` | Overnight or weekend. Only 4:00 AM–8:00 PM New York time on weekdays is traded, and only with `extended_hours: true` |
| `market session is extended` | Pre-market or after-hours with `extended_hours: false` |
| `signal is stale` | Arrived more than `MAX_SIGNAL_AGE_S` after it fired |
| `deviates …% from live` | The alert price is too far from Webull's live price |
| `kill switch is on` | See section 3 |
| `already at target` | Nothing to do: the position already matches the signal |

### HTTPS certificate

**Check the certificate was issued and renews.** It should show "certificate obtained successfully".

```bash
cd /opt/gateway && docker compose logs caddy | grep -iE "certificate obtained|error" | tail -5
```

### Webull sign-in (2FA token)

If the Webull calls fail with an authentication error, run the account check and approve
the sign-in in the Webull app if it asks:

```bash
cd /opt/gateway && docker compose exec gateway python -m scripts.check_webull SOXL
```

---

## 10. Updating and maintenance

**Install a new version of the code** (after changes are pushed). `.env` and `strategies.yaml` are not touched.

```bash
cd /opt/gateway && git pull && docker compose up -d --build && sleep 5 && curl -s localhost:8000/health
```

**Back up the trade journal database.** The copy goes in your home folder.

```bash
cd /opt/gateway && docker compose exec gateway python -c "import sqlite3; s=sqlite3.connect('/data/trading.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()" && cp data/backup.db ~/trading-$(date +%F).db && ls -l ~/trading-*.db
```

**Copy the backup to your own computer.** Run this on your computer, not the server, and change the date to match.

```bash
scp -i ~/.ssh/webull_gateway ubuntu@trade.mdtrade.app:~/trading-2026-11-17.db .
```

**Disk space.** If it's over 80%, clean old Docker images with the next command.

```bash
df -h / && docker system df
```

```bash
docker image prune -f
```

**Reboot the server.** Everything starts again by itself, and Telegram reports DOWN, then RECOVERED.

```bash
sudo reboot
```
