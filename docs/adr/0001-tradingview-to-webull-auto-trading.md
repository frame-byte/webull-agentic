# ADR-0001: Automated trading from TradingView alerts to Webull

**Status:** Accepted
**Date:** 2026-11-13
**Deciders:** Account owner (sole trader / operator)

## Context

I run strategies and alerts in TradingView for a handful of US stocks. When an alert
fires, I want my Webull account to buy or sell the stock automatically, following the
signal, without me in the loop.

Facts that shape the design:

| Constraint | Detail | Impact |
|---|---|---|
| **TradingView output** | The only machine-readable output of an alert is a **webhook**: an HTTP POST of the alert message to a URL. Only ports 80 and 443 are allowed. You can't set custom headers, the request times out after about 3 s, and failed deliveries aren't retried. Requests come from a fixed set of TradingView IPs (see the TradingView docs). | We need a public HTTPS endpoint that's always on. It must reply quickly and authenticate the caller from the message body plus the source IP. |
| **Webhook setup** | Webhooks need 2FA on the TradingView account. They can't be created through the TradingView MCP connector, so they must be set up on tradingview.com. | Setting up alerts is a manual step in the TradingView UI. |
| **Webull "agentic" (MCP) trading** | `place_stock_instruction` **does not place an order**. It creates an *instruction* that the owner must confirm in the Webull app. It only supports **LIMIT** orders, one per call. The account (`…7071`) has `Trading` authorization, but every instruction still needs a tap in the app. | The agentic/MCP path **can't give fully automatic execution**. It works for a "one-tap confirm" mode, not hands-off trading. |
| **Webull OpenAPI** | Webull runs a separate developer OpenAPI (app key and secret, official Python/Java SDKs) that places, cancels, and queries orders directly. You apply for access in the Webull developer portal. | This is the only first-party route to orders that execute without a person confirming each one. **Check that it's available for this account type before building.** |
| **Money at risk** | A bug, a duplicate webhook, a repainting indicator, or a forged request can all turn into real trades. | Safety controls are a core requirement, not an add-on. |

Non-functional targets (single user, low volume):
- **Latency:** from alert to order submitted in under 2 s at p95. These are swing or intraday-bar strategies, not HFT.
- **Volume:** under 100 signals a day.
- **Availability:** always up during market hours. A missed signal is worse than a slow one, and a *duplicated* signal is worse than a missed one.
- **Cost:** under $10 a month in infrastructure.
- **Auditability:** every signal, decision, and order is logged with the reason.

## Decision

Build a small, deterministic, self-hosted **Signal Gateway**. It receives TradingView
webhooks, runs every signal through a **risk engine**, and executes through a
**pluggable broker adapter**. The default adapter is the Webull OpenAPI, for fully
automatic execution. The Webull agentic MCP is a second adapter for a "confirm-in-app"
mode. LLM agents (Claude) sit **outside the order path**. They're used for monitoring,
daily review, and strategy work, never to decide or place an order in real time.

```
TradingView strategy/alert
   │  HTTPS POST (JSON message, shared secret in body)
   ▼
┌──────────────────────── Signal Gateway (FastAPI, 1 container) ────────────────────────┐
│ 1. Ingress: IP allowlist → secret check → schema validation → idempotency key → 200 OK│
│ 2. Signal store (SQLite/Postgres): append-only signals, decisions, orders, fills      │
│ 3. Worker: Risk engine ──reject──► log + notify                                        │
│                 │ accept                                                               │
│                 ▼                                                                      │
│    Order builder (target position → delta qty, limit price = last ± slippage band)   │
│                 ▼                                                                      │
│    BrokerAdapter interface                                                             │
│      ├─ DryRunAdapter        (log only)                                                │
│      ├─ WebullOpenApiAdapter (direct orders — AUTO mode)                               │
│      └─ WebullAgenticAdapter (MCP instruction, confirm in app — CONFIRM mode)          │
│ 4. Reconciler (every 30–60 s): order status, fills, positions vs. expected state       │
│ 5. Notifier: Telegram/Pushover/email on fill, reject, error, drift                     │
└───────────────────────────────────────────────────────────────────────────────────────┘
          ▲ read-only
   Claude (optional): daily P&L review, anomaly triage, strategy backtests via TV MCP
```

### Signal contract (TradingView alert message)

Strategy alerts should send the strategy's **target position**, not a bare "buy" or "sell".
That way a duplicated or missed signal corrects itself on the next one instead of
compounding:

```json
{
  "secret": "{{SECRET_FROM_ENV}}",
  "strategy_id": "ema-cross-v2",
  "ticker": "{{ticker}}",
  "exchange": "{{exchange}}",
  "action": "{{strategy.order.action}}",
  "contracts": "{{strategy.order.contracts}}",
  "position_size": "{{strategy.position_size}}",
  "market_position": "{{strategy.market_position}}",
  "price": "{{close}}",
  "bar_time": "{{time}}",
  "fired_at": "{{timenow}}",
  "order_id": "{{strategy.order.id}}"
}
```

- **Idempotency key:** `strategy_id + ticker + bar_time + order_id`. A duplicate key returns 200 and is ignored.
- **Sizing:** the gateway maps `position_size` to *real* shares through a per-strategy config (fixed shares, fixed dollars, or % of equity). TradingView's simulated equity never sets live size.
- **Repainting:** alerts are set to "Once Per Bar Close" and strategies use `calc_on_every_tick=false`.

### Risk engine (all checks must pass; any failure rejects the signal and notifies)

1. **Kill switch:** a global and a per-strategy `enabled` flag, which can be flipped from the phone.
2. **Allowlist:** only configured `(strategy_id, ticker)` pairs.
3. **Session:** regular hours only by default; extended hours is opt-in per strategy.
4. **Staleness:** reject if `now - fired_at > 60 s`.
5. **Price sanity:** compare the signal price with the live Webull quote. Reject if they differ by more than X%.
6. **Exposure:** max shares and max notional per symbol, max total gross exposure, and a max number of orders per day.
7. **Loss limit:** if realized plus unrealized P&L for the day falls below −Y%, trip the kill switch for the rest of the day.
8. **Long-only guard:** never sell more than the shares held unless the strategy allows shorting.
9. **Single in-flight order:** one open order per symbol; a new signal cancels and replaces it.

### Order execution

- Use **marketable LIMIT** orders by default: buy at `ask × (1 + band)`, sell at `bid × (1 − band)`, with a band of about 0.2–0.5%. This bounds slippage and fits the agentic adapter, which only accepts LIMIT orders.
- Orders are `DAY`. Anything unfilled after N seconds is cancelled and re-priced up to K times, then given up and notified.

## Options Considered

### Option A: Self-hosted gateway → Webull OpenAPI (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Medium: about 1–2k lines of Python, one container |
| Cost | About $5/month VPS or Cloud Run/Fly.io free tier, plus market data already covered by Webull |
| Scalability | Far more than needed (under 100 signals a day) |
| Team familiarity | Standard Python, FastAPI, and SQLite |
| Automation | **Fully automatic** |

**Pros:** truly hands-off; deterministic; full control of risk rules; no third party holds broker credentials; complete audit log; adapters make paper and dry-run modes trivial.
**Cons:** I own uptime and security; I must get Webull OpenAPI access; I maintain the code.

### Option B: TradingView → Claude agent session → Webull agentic MCP

A webhook wakes a Claude session (for example `watch_url` or a Routine), which reads
the alert and calls `place_stock_instruction`.

| Dimension | Assessment |
|---|---|
| Complexity | Low to build, high to make reliable |
| Cost | LLM usage for every signal |
| Scalability | Fine for the volume |
| Automation | **Not automatic**: each order needs confirmation in the Webull app |

**Pros:** almost no code; uses the account's existing agentic authorization; a human
approves every trade.
**Cons:** doesn't meet the core requirement, because a person must confirm each order.
It puts a non-deterministic LLM in the order path. Latency ranges from seconds to
minutes. Session lifetime and wake-up are not built for always-on trading. It accepts
LIMIT orders only.

### Option C: Third-party bridge (for example TradersPost or a similar webhook-to-broker service)

| Dimension | Assessment |
|---|---|
| Complexity | Lowest: configuration only |
| Cost | About $30–100/month subscription |
| Automation | Automatic, *if* the service supports Webull for this account (check first) |

**Pros:** fastest to go live; the vendor runs uptime and the broker integration.
**Cons:** broker credentials sit with a third party; their risk controls are limited
and generic; vendor lock-in; Webull support varies and may not exist.

## Trade-off Analysis

- **The deciding factor is A vs. B on the "automatic" requirement.** The agentic MCP is
  deliberately human-in-the-loop, so B can't meet the goal however well it's built.
  It stays useful as a **CONFIRM mode adapter**: helpful while gaining trust in a new
  strategy, or as a fallback if OpenAPI access is denied.
- **A vs. C** trades build effort against control and credential custody. With real
  money and custom sizing and loss rules, owning the risk engine is worth roughly a
  week of work. C is a reasonable short-term bridge if OpenAPI access is delayed.
- **Target-position signals vs. action signals:** with target positions, the system
  can recover from a missed or duplicated webhook (TradingView doesn't retry). The cost
  is one positions lookup per signal.
- **LLM in the loop:** keeping Claude out of the hot path makes order behavior
  reproducible and testable. Claude adds the most value *around* the loop: backtesting
  strategies through the TradingView MCP, reviewing the daily journal, and explaining
  rejects.

## Consequences

**Easier**
- Adding a new stock or strategy is a config entry plus a TradingView alert.
- Switching between dry-run, confirm, and auto modes per strategy without code changes.
- Post-trade analysis, because every signal, decision, and order is in one store.

**Harder**
- I'm responsible for uptime, TLS, secret rotation, and patching.
- TradingView and Webull can disagree: partial fills, rejected orders, or manual trades
  in the app. The reconciler must catch this, and I must decide how to resolve it.
- Webhooks are fire-and-forget. If the gateway is down when an alert fires, the signal
  is lost. Target-position semantics plus a "position drift" alert mitigate this.

**Revisit**
- Whether Webull OpenAPI access is granted. If not, fall back to Option C or run in
  CONFIRM mode.
- Wash-sale and pattern-day-trader (PDT) rules if the account is under $25k: add a
  day-trade counter to the risk engine.
- Moving from SQLite to Postgres only if more than one gateway instance is needed.

## Action Items

1. [ ] Apply for Webull OpenAPI access (app key and secret) and confirm it can place live orders for account `…7071`. Confirm paper-trading support.
2. [ ] Scaffold the repo: `gateway/` (FastAPI ingress), `core/` (risk engine, order builder), `brokers/` (DryRun, WebullOpenApi, WebullAgentic), `reconciler/`, `config/strategies.yaml`.
3. [ ] Put secrets (webhook secret, Webull keys, notifier token) in the host's secret manager, never in the repo. Rotate the webhook secret if a TradingView alert message ever leaks.
4. [ ] Implement ingress: IP allowlist, secret check, schema, idempotency, and a 200 response in under 500 ms, with work queued.
5. [ ] Implement the risk engine and write unit tests for every rule: duplicate, stale, oversize, after-hours, loss limit, kill switch.
6. [ ] Implement the reconciler and notifier (fills, rejects, drift, gateway heartbeat).
7. [ ] Deploy behind HTTPS on port 443 (Fly.io, Cloud Run with min instances set to 1, or a VPS with Caddy). Add an external uptime check.
8. [ ] Update the TradingView strategies: "Once Per Bar Close", the JSON message template above, and the webhook URL (set up on tradingview.com, since 2FA is required).
9. [ ] Roll out in stages: **DryRun for 1–2 weeks** (compare against the TradingView strategy tester), then **CONFIRM mode** through the agentic adapter, then **AUTO with 1-share sizing**, then full sizing.
10. [ ] Optional: a scheduled Claude routine that reads the journal after the close, summarizes P&L and rejects, and flags drift between TradingView and Webull.
