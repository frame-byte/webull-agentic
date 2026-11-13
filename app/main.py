"""HTTP entrypoint: TradingView webhook + small admin API.

Run:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

from .brokers.dryrun import DryRunBroker
from .config import Settings
from .executor import Executor
from .models import Signal
from .notify import Notifier
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("gateway")


def build_executor(settings: Settings, broker=None) -> Executor:
    if broker is None:
        has_keys = settings.webull_app_key and settings.webull_app_secret and settings.webull_account_id
        real = None
        if has_keys:
            from .brokers.webull_openapi import WebullOpenApiBroker
            real = WebullOpenApiBroker(settings.webull_app_key, settings.webull_app_secret,
                                       settings.webull_account_id, settings.webull_region, settings.webull_endpoint)
        if settings.mode == "live":
            broker = real
        else:
            # Dry run with live Webull quotes when keys exist; positions are simulated.
            broker = DryRunBroker(quote_source=real)
    return Executor(settings, Store(settings.db_path), broker,
                    Notifier(settings.telegram_bot_token, settings.telegram_chat_id))


def create_app(settings: Optional[Settings] = None, executor: Optional[Executor] = None,
               reconcile_interval_s: float = 30) -> FastAPI:
    settings = settings or Settings.from_env()
    ex = executor or build_executor(settings)

    async def reconcile_loop():
        while True:
            await asyncio.sleep(reconcile_interval_s)
            try:
                await asyncio.to_thread(ex.reconcile, time.time())
            except Exception:
                log.exception("reconcile failed")

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(reconcile_loop()) if reconcile_interval_s > 0 else None
        ex.notifier.send(f"Gateway started in {settings.mode.upper()} mode "
                         f"({len(settings.strategies)} strategies, broker={ex.broker.name})")
        yield
        if task:
            task.cancel()

    app = FastAPI(title="TradingView → Webull gateway", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.executor = ex

    def client_ip(request: Request) -> str:
        if settings.trust_proxy_headers:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""

    def check_admin(token: Optional[str]) -> None:
        if not token or not hmac.compare_digest(token, settings.webhook_secret):
            raise HTTPException(401, "unauthorized")

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks):
        ip = client_ip(request)
        if settings.enforce_ip_allowlist and ip not in settings.ip_allowlist:
            log.warning("rejected webhook from %s", ip)
            raise HTTPException(403, "forbidden")

        raw = await request.body()
        if len(raw) > 8192:
            raise HTTPException(413, "payload too large")
        try:
            payload = json.loads(raw)  # TradingView may send text/plain even for JSON bodies
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except ValueError:
            raise HTTPException(400, "body must be a JSON object")

        if not hmac.compare_digest(str(payload.get("secret", "")), settings.webhook_secret):
            log.warning("bad secret from %s", ip)
            raise HTTPException(401, "unauthorized")
        try:
            sig = Signal.model_validate(payload)
        except ValidationError as e:
            raise HTTPException(422, f"invalid signal: {e.errors(include_url=False)}")

        signal_id = ex.store.insert_signal(sig.idempotency_key(), sig.strategy_id, sig.ticker, payload)
        if signal_id is None:
            return {"status": "duplicate"}

        # Reply to TradingView immediately (it times out after ~3s); trade in the background.
        background.add_task(asyncio.to_thread, ex.process, signal_id, sig)
        return {"status": "accepted", "signal_id": signal_id}

    @app.get("/health")
    def health():
        return {"ok": True, "mode": settings.mode, "broker": ex.broker.name, "kill_switch": ex.killed()}

    @app.post("/admin/kill")
    def kill(on: bool = True, x_admin_token: Optional[str] = Header(None)):
        check_admin(x_admin_token)
        ex.set_kill(on, "manual")
        return {"kill_switch": ex.killed()}

    @app.get("/admin/journal")
    def journal(limit: int = 50, x_admin_token: Optional[str] = Header(None)):
        check_admin(x_admin_token)
        return {"signals": ex.store.recent_signals(limit), "orders": ex.store.recent_orders(limit)}

    return app

