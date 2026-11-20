"""Runtime settings (env vars) and per-strategy config (YAML)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

# Published TradingView webhook source IPs.
TRADINGVIEW_IPS = ["52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"]


@dataclass
class StrategyConfig:
    strategy_id: str
    symbols: list[str]
    enabled: bool = True
    # How to turn a TradingView signal into a real share count.
    #   fixed_shares: long target = `shares`
    #   fixed_dollars: long target = floor(`dollars` / price)
    #   mirror: target = TradingView strategy.position_size * `multiplier`
    #   percent_equity: long target = floor(`percent`% of account net liquidation / price)
    sizing: Literal["fixed_shares", "fixed_dollars", "mirror", "percent_equity"] = "fixed_shares"
    shares: float = 1
    dollars: float = 0
    multiplier: float = 1.0
    percent: float = 0              # for percent_equity; 1-100, never uses margin
    trailing_stop_pct: float = 0    # >0: broker-side trailing stop this % below the high (long only)
    allow_short: bool = False
    extended_hours: bool = False
    max_shares: float = 100          # absolute position cap per symbol
    max_notional: float = 5_000      # $ cap per symbol position


@dataclass
class Settings:
    mode: Literal["dryrun", "live"] = "dryrun"
    webhook_secret: str = ""
    db_path: str = "data/trading.db"
    strategies_path: str = "config/strategies.yaml"
    ip_allowlist: list[str] = field(default_factory=lambda: list(TRADINGVIEW_IPS))
    enforce_ip_allowlist: bool = True
    trust_proxy_headers: bool = False   # true when behind a reverse proxy that sets X-Forwarded-For

    # Global risk
    max_signal_age_s: int = 60
    max_price_deviation_pct: float = 1.5
    limit_band_pct: float = 0.3         # marketable-limit slippage band
    max_orders_per_day: int = 20
    max_daily_loss_pct: float = 3.0     # trips the kill switch
    order_timeout_s: int = 120          # cancel unfilled orders after this

    # Webull OpenAPI
    webull_app_key: str = ""
    webull_app_secret: str = ""
    webull_account_id: str = ""
    webull_region: str = "us"
    webull_endpoint: str = ""           # optional override, e.g. api.webull.com

    # Notifications (optional)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    strategies: dict[str, StrategyConfig] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        s = cls(
            mode=e("MODE", "dryrun"),  # type: ignore[arg-type]
            webhook_secret=e("WEBHOOK_SECRET", ""),
            db_path=e("DB_PATH", "data/trading.db"),
            strategies_path=e("STRATEGIES_PATH", "config/strategies.yaml"),
            enforce_ip_allowlist=e("ENFORCE_IP_ALLOWLIST", "true").lower() == "true",
            trust_proxy_headers=e("TRUST_PROXY_HEADERS", "false").lower() == "true",
            max_signal_age_s=int(e("MAX_SIGNAL_AGE_S", "60")),
            max_price_deviation_pct=float(e("MAX_PRICE_DEVIATION_PCT", "1.5")),
            limit_band_pct=float(e("LIMIT_BAND_PCT", "0.3")),
            max_orders_per_day=int(e("MAX_ORDERS_PER_DAY", "20")),
            max_daily_loss_pct=float(e("MAX_DAILY_LOSS_PCT", "3.0")),
            order_timeout_s=int(e("ORDER_TIMEOUT_S", "120")),
            webull_app_key=e("WEBULL_APP_KEY", ""),
            webull_app_secret=e("WEBULL_APP_SECRET", ""),
            webull_account_id=e("WEBULL_ACCOUNT_ID", ""),
            webull_region=e("WEBULL_REGION", "us"),
            webull_endpoint=e("WEBULL_ENDPOINT", ""),
            telegram_bot_token=e("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=e("TELEGRAM_CHAT_ID", ""),
        )
        extra_ips = e("EXTRA_ALLOWED_IPS", "")
        if extra_ips:
            s.ip_allowlist += [ip.strip() for ip in extra_ips.split(",") if ip.strip()]
        s.strategies = load_strategies(s.strategies_path)
        s.validate()
        return s

    def validate(self) -> None:
        if self.mode not in ("dryrun", "live"):
            raise ValueError(f"MODE must be dryrun or live, got {self.mode!r}")
        if len(self.webhook_secret) < 16:
            raise ValueError("WEBHOOK_SECRET must be set and at least 16 characters")
        if self.mode == "live" and not (self.webull_app_key and self.webull_app_secret and self.webull_account_id):
            raise ValueError("live mode needs WEBULL_APP_KEY, WEBULL_APP_SECRET and WEBULL_ACCOUNT_ID")


def load_strategies(path: str) -> dict[str, StrategyConfig]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"strategy config not found: {path} (copy config/strategies.example.yaml)")
    raw = yaml.safe_load(p.read_text()) or {}
    out: dict[str, StrategyConfig] = {}
    for sid, cfg in (raw.get("strategies") or {}).items():
        cfg = dict(cfg)
        cfg["symbols"] = [s.upper() for s in cfg.get("symbols", [])]
        st = StrategyConfig(strategy_id=sid, **cfg)
        if st.sizing not in ("fixed_shares", "fixed_dollars", "mirror", "percent_equity"):
            raise ValueError(f"{sid}: unknown sizing {st.sizing!r}")
        if st.sizing == "percent_equity" and not 0 < st.percent <= 100:
            raise ValueError(f"{sid}: percent_equity needs 0 < percent <= 100 (got {st.percent})")
        if st.trailing_stop_pct and not 0.5 <= st.trailing_stop_pct <= 50:
            raise ValueError(f"{sid}: trailing_stop_pct must be between 0.5 and 50 (got {st.trailing_stop_pct})")
        out[sid] = st
    return out
