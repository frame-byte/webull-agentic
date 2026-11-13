"""Best-effort notifications. Never raises: a notifier outage must not block trading."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, telegram_bot_token: str = "", telegram_chat_id: str = ""):
        self.token = telegram_bot_token
        self.chat_id = telegram_chat_id
        self.sent: list[str] = []  # handy in tests

    def send(self, text: str) -> None:
        self.sent.append(text)
        log.info("NOTIFY %s", text)
        if not (self.token and self.chat_id):
            return
        try:
            httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                       json={"chat_id": self.chat_id, "text": text}, timeout=5)
        except Exception:
            log.exception("telegram notify failed")
