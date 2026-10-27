"""Signed outbound webhooks for run lifecycle events.

Each delivery is POSTed as JSON with headers:
  X-Argus-Event: <event name>
  X-Argus-Delivery: <unique id, stable across retries>
  X-Argus-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, "<ts>.<raw body>")>
Receivers should verify the signature and reject timestamps older than 5 minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import orjson
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.logging import get_logger
from argus.core.netguard import guarded_client
from argus.db.models import Webhook, WebhookDelivery

log = get_logger(__name__)

EVENTS = ("run.completed", "run.failed", "run.halted", "run.cancelled", "approval.requested", "run.started")
RETRY_DELAYS = (1, 5, 30)
DISABLE_AFTER_FAILURES = 20


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    expected = sign(secret, body, ts).split("v1=")[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


class WebhookDispatcher:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        client_factory: Any = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.box = SecretBox(settings)
        self.client_factory = client_factory or (
            lambda: guarded_client(
                settings.connections_allow_private_urls, timeout=httpx.Timeout(10.0, connect=5.0)
            )
        )
        self.sleep = sleep
        self.tasks: set[asyncio.Task[None]] = set()

    def emit(self, owner_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        """Schedule deliveries without blocking the caller."""
        task = asyncio.create_task(self._fanout(owner_id, event, data))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _fanout(self, owner_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        async with self.sessionmaker() as s:
            hooks = list(
                (
                    await s.execute(
                        select(Webhook).where(Webhook.owner_id == owner_id, Webhook.enabled.is_(True))
                    )
                ).scalars()
            )
        targets = [h for h in hooks if event in (h.events or []) or "*" in (h.events or [])]
        await asyncio.gather(*(self.deliver(h, event, data) for h in targets))

    async def deliver(self, hook: Webhook, event: str, data: dict[str, Any]) -> bool:
        delivery_id = f"dlv_{uuid.uuid4().hex}"
        body = orjson.dumps(
            {"id": delivery_id, "event": event, "created_at": datetime.now(UTC).isoformat(), "data": data}
        )
        secret = self.box.decrypt(hook.secret_enc)
        ok = False
        for attempt, delay in enumerate((0, *RETRY_DELAYS), start=1):
            if delay:
                await self.sleep(delay)
            t0 = time.monotonic()
            status: int | None = None
            error = ""
            try:
                async with self.client_factory() as client:
                    resp = await client.post(
                        hook.url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Argus-Webhooks/1",
                            "X-Argus-Event": event,
                            "X-Argus-Delivery": delivery_id,
                            "X-Argus-Signature": sign(secret, body),
                        },
                        follow_redirects=False,
                    )
                status = resp.status_code
                ok = 200 <= status < 300
            except httpx.HTTPError as exc:
                error = type(exc).__name__
            await self._log(
                hook, event, delivery_id, attempt, status, ok, error, int((time.monotonic() - t0) * 1000)
            )
            if ok or (status is not None and 400 <= status < 500 and status not in (408, 429)):
                break  # success, or a client error retrying will not fix
        await self._update(hook, ok, status if not error else error)
        return ok

    async def _log(
        self,
        hook: Webhook,
        event: str,
        delivery_id: str,
        attempt: int,
        status: int | None,
        ok: bool,
        error: str,
        duration_ms: int,
    ) -> None:
        async with self.sessionmaker() as s:
            s.add(
                WebhookDelivery(
                    webhook_id=hook.id,
                    ts=datetime.now(UTC),
                    event=event,
                    delivery_id=delivery_id,
                    attempt=attempt,
                    status_code=status,
                    ok=ok,
                    error=error[:300],
                    duration_ms=duration_ms,
                )
            )
            await s.commit()

    async def _update(self, hook: Webhook, ok: bool, last: Any) -> None:
        async with self.sessionmaker() as s:
            row = await s.get(Webhook, hook.id)
            if row is None:
                return
            row.failure_count = 0 if ok else row.failure_count + 1
            row.last_status = str(last)[:40] if last is not None else ""
            row.last_delivery_at = datetime.now(UTC)
            if row.failure_count >= DISABLE_AFTER_FAILURES:
                row.enabled = False
                log.warning("webhooks.disabled_after_failures", webhook=str(hook.id))
            await s.commit()

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def reset_failures(self, hook_id: uuid.UUID) -> None:
        async with self.sessionmaker() as s:
            await s.execute(update(Webhook).where(Webhook.id == hook_id).values(failure_count=0))
            await s.commit()
