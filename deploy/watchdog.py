"""Gateway watchdog: polls /health and sends Telegram alerts. Standard library only.

Runs as its own container (see docker-compose.yml) so it keeps working when the gateway crashes.

Alerts:
  - DOWN after FAIL_THRESHOLD consecutive failed checks, then a reminder every REMIND_MIN minutes
  - RECOVERED with the outage length
  - kill switch turned on/off, or mode changed (e.g. live -> dryrun)
  - a daily heartbeat on weekdays, so silence means the whole server is down

  python deploy/watchdog.py          # run forever
  python deploy/watchdog.py --test   # send one test message and exit
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HEALTH_URL = os.environ.get("WATCHDOG_HEALTH_URL", "http://gateway:8000/health")
INTERVAL_S = int(os.environ.get("WATCHDOG_INTERVAL_S", "30"))
FAIL_THRESHOLD = int(os.environ.get("WATCHDOG_FAIL_THRESHOLD", "3"))
REMIND_MIN = int(os.environ.get("WATCHDOG_REMIND_MIN", "30"))
HEARTBEAT_HOUR_ET = int(os.environ.get("WATCHDOG_HEARTBEAT_HOUR_ET", "9"))
HOST = os.environ.get("DOMAIN", "gateway")

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # no tz database in the image: fixed EST is close enough for a heartbeat
    ET = timezone(timedelta(hours=-5))


def fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m {int(seconds % 60)}s"


class Watchdog:
    """Pure state machine: feed it health results, get back messages to send."""

    def __init__(self, fail_threshold: int = FAIL_THRESHOLD, remind_min: int = REMIND_MIN,
                 heartbeat_hour_et: int = HEARTBEAT_HOUR_ET):
        self.fail_threshold = fail_threshold
        self.remind_s = remind_min * 60
        self.heartbeat_hour = heartbeat_hour_et
        self.fails = 0
        self.down_since: float | None = None
        self.last_remind = 0.0
        self.last_health: dict | None = None
        self.last_heartbeat_day: str | None = None

    def step(self, now: float, health: dict | None, error: str = "") -> list[str]:
        msgs: list[str] = []
        if health is None or not health.get("ok"):
            self.fails += 1
            if self.down_since is None and self.fails >= self.fail_threshold:
                self.down_since = now - (self.fails - 1) * INTERVAL_S
                self.last_remind = now
                msgs.append(f"🔴 GATEWAY DOWN ({HOST}): {self.fails} failed health checks. {error}\n"
                            "Webhooks fail with 502 and TradingView does not retry.\n"
                            "Check: cd /opt/gateway && docker compose ps && docker compose logs --tail 40 gateway")
            elif self.down_since is not None and now - self.last_remind >= self.remind_s:
                self.last_remind = now
                msgs.append(f"🔴 Gateway still DOWN for {fmt_duration(now - self.down_since)}. {error}")
            return msgs

        if self.down_since is not None:
            msgs.append(f"🟢 Gateway RECOVERED after {fmt_duration(now - self.down_since)} "
                        f"(mode={health.get('mode')}, kill_switch={health.get('kill_switch')})")
        self.fails, self.down_since = 0, None

        prev = self.last_health
        if prev is not None:
            if health.get("kill_switch") != prev.get("kill_switch"):
                msgs.append("⛔ Kill switch is ON: no orders will be placed." if health.get("kill_switch")
                            else "✅ Kill switch is OFF: trading resumed.")
            if health.get("mode") != prev.get("mode"):
                msgs.append(f"⚠️ Gateway mode changed: {prev.get('mode')} -> {health.get('mode')}")
        self.last_health = health

        t = datetime.fromtimestamp(now, ET)
        day = t.strftime("%Y-%m-%d")
        if t.weekday() < 5 and t.hour >= self.heartbeat_hour and self.last_heartbeat_day != day:
            self.last_heartbeat_day = day
            msgs.append(f"✅ Daily check: gateway up, mode={health.get('mode')}, "
                        f"kill_switch={health.get('kill_switch')}. No message tomorrow = server is down.")
        return msgs


def check_health() -> tuple[dict | None, str]:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
            return json.loads(r.read()), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, type(e).__name__ + (f": {e.reason}" if hasattr(e, "reason") else "")


def send_telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), text.replace("\n", " | "), flush=True)
    if not (token and chat):
        print("  (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: not sent)", flush=True)
        return
    body = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return
        except Exception as e:
            print(f"  telegram send failed ({type(e).__name__}), attempt {attempt + 1}/3", flush=True)
            time.sleep(2 ** attempt)


def main() -> None:
    if "--test" in sys.argv:
        send_telegram(f"🧪 Watchdog test message from {HOST}. Alerts are working.")
        return
    wd = Watchdog()
    # Don't send a heartbeat just because the watchdog (re)started mid-day.
    wd.last_heartbeat_day = datetime.now(ET).strftime("%Y-%m-%d")
    send_telegram(f"👀 Watchdog started for {HOST} (checks every {INTERVAL_S}s).")
    while True:
        health, err = check_health()
        for m in wd.step(time.time(), health, err):
            send_telegram(m)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
