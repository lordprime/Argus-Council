"""Signed outbound webhooks for run lifecycle events.

Each delivery is POSTed as JSON with headers:
  X-Argus-Event: <event name>
  X-Argus-Delivery: <unique id, stable across retries>
  X-Argus-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, "<ts>.<raw body>")>
Receivers should verify the signature and reject timestamps older than 5 minutes.

Delivery is at-least-once through a durable outbox (``webhook_outbox``):

* ``emit`` writes one outbox row per subscribed hook and wakes the worker. Nothing is held only
  in memory, so a restart (or a SIGKILL) resumes pending deliveries and retries.
* The worker claims due rows with an optimistic update that also sets a lease, makes one attempt
  per row, and either deletes the row (success, a permanent 4xx, retries exhausted, or older than
  ``MAX_AGE_S``) or schedules the next retry. A process that dies mid-attempt leaves the lease to
  expire, and any process picks the row up again.
* Each hook holds at most ``MAX_PENDING_PER_HOOK`` pending rows; beyond that new events are
  dropped and an ``outbox_full`` row is written to the delivery log.
* ``shutdown(grace_s)`` stops the worker and waits at most ``grace_s`` seconds for in-flight
  work; whatever is cut short stays in the outbox.
* Every attempt and every failure to deliver is written to ``webhook_deliveries`` and logged;
  ``stats`` counts errors in-process.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import orjson
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.logging import get_logger
from argus.core.metrics import WEBHOOK_DELIVERIES, WEBHOOK_DELIVERY_SECONDS
from argus.core.netguard import guarded_client
from argus.db.models import Webhook, WebhookDelivery, WebhookOutbox

log = get_logger(__name__)

EVENTS = ("run.completed", "run.failed", "run.halted", "run.cancelled", "approval.requested", "run.started")
RETRY_DELAYS = (1, 5, 30)  # seconds before attempts 2, 3 and 4
DISABLE_AFTER_FAILURES = 20
MAX_PENDING_PER_HOOK = 1000
MAX_AGE_S = 24 * 3600  # a delivery not made within a day is dropped
LEASE_S = 60  # a claimed row is invisible to other workers for this long
BATCH = 50
CONCURRENCY = 8
POLL_S = 2.0  # rows written by other processes (or due retries) are found within this
ATTEMPT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
TEST_TIMEOUT = httpx.Timeout(5.0)


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


def _permanent(status: int | None) -> bool:
    """A client error that retrying will not fix."""
    return status is not None and 400 <= status < 500 and status not in (408, 429)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WebhookDispatcher:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.box = SecretBox(settings)
        self.client_factory = client_factory or (
            lambda: guarded_client(settings.connections_allow_private_urls, timeout=ATTEMPT_TIMEOUT)
        )
        self.now = now
        # Enqueue tasks (and, shared by the run manager, in-app notification tasks).
        self.tasks: set[asyncio.Task[None]] = set()
        self.stats: Counter[str] = Counter()
        self._client: httpx.AsyncClient | None = None
        self._client_from: Any = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ producer
    def emit(self, owner_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        """Queue deliveries without blocking the caller."""
        task = asyncio.create_task(self._enqueue_logged(owner_id, event, data))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _enqueue_logged(self, owner_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        try:
            await self.enqueue(owner_id, event, data)
        except Exception:
            self.stats["enqueue_failed"] += 1
            log.exception("webhooks.enqueue_failed", owner=str(owner_id), webhook_event=event)

    async def enqueue(self, owner_id: uuid.UUID, event: str, data: dict[str, Any]) -> int:
        """Write one outbox row per enabled hook subscribed to ``event``. Returns rows written."""
        now = self.now()
        written = 0
        async with self.sessionmaker() as s:
            hooks = (
                await s.execute(
                    select(Webhook.id, Webhook.events).where(
                        Webhook.owner_id == owner_id, Webhook.enabled.is_(True)
                    )
                )
            ).all()
            targets = [hid for hid, events in hooks if event in (events or []) or "*" in (events or [])]
            if not targets:
                return 0
            pending = dict(
                (
                    await s.execute(
                        select(WebhookOutbox.webhook_id, func.count(WebhookOutbox.id))
                        .where(WebhookOutbox.webhook_id.in_(targets))
                        .group_by(WebhookOutbox.webhook_id)
                    )
                ).all()
            )
            for hid in targets:
                delivery_id = f"dlv_{uuid.uuid4().hex}"
                if pending.get(hid, 0) >= MAX_PENDING_PER_HOOK:
                    self.stats["dropped"] += 1
                    log.warning("webhooks.outbox_full", webhook=str(hid), webhook_event=event)
                    s.add(self._delivery_row(hid, event, delivery_id, 0, None, False, "outbox_full", 0))
                    continue
                body = orjson.dumps(
                    {"id": delivery_id, "event": event, "created_at": now.isoformat(), "data": data}
                ).decode()
                s.add(
                    WebhookOutbox(
                        webhook_id=hid,
                        event=event,
                        delivery_id=delivery_id,
                        body=body,
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
                written += 1
            await s.commit()
        if written:
            self._wake.set()
        return written

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopping.clear()
            self._worker = asyncio.create_task(self._loop(), name="webhooks")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                busy = await self.process_due()
            except Exception:
                self.stats["errors"] += 1
                log.exception("webhooks.worker_failed")
                busy = 0
            if not busy and not self._stopping.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), POLL_S)

    async def process_due(self, limit: int = BATCH) -> int:
        """Claim due rows and make one attempt each. Returns the number of rows handled."""
        async with self._lock:
            now = self.now()
            async with self.sessionmaker() as s:
                due = (
                    await s.execute(
                        select(WebhookOutbox.id, WebhookOutbox.attempts)
                        .where(WebhookOutbox.next_attempt_at <= now)
                        .order_by(WebhookOutbox.next_attempt_at)
                        .limit(limit)
                    )
                ).all()
                claimed = []
                for oid, attempts in due:
                    res = await s.execute(
                        update(WebhookOutbox)
                        .where(WebhookOutbox.id == oid, WebhookOutbox.attempts == attempts)
                        .values(attempts=attempts + 1, next_attempt_at=now + timedelta(seconds=LEASE_S))
                    )
                    if res.rowcount == 1:  # type: ignore[attr-defined]
                        claimed.append(oid)
                await s.commit()
                if not claimed:
                    return 0
                expired_col = (WebhookOutbox.created_at < now - timedelta(seconds=MAX_AGE_S)).label("expired")
                jobs = (
                    await s.execute(
                        select(WebhookOutbox, Webhook, expired_col)
                        .join(Webhook, Webhook.id == WebhookOutbox.webhook_id)
                        .where(WebhookOutbox.id.in_(claimed))
                    )
                ).all()
            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(row: WebhookOutbox, hook: Webhook, expired: bool) -> None:
                async with sem:
                    await self._attempt(row, hook, bool(expired))

            await asyncio.gather(*(one(*job) for job in jobs))
            return len(jobs)

    async def _attempt(self, row: WebhookOutbox, hook: Webhook, expired: bool) -> None:
        try:
            if not hook.enabled:
                await self._finish(row, None)  # disabled since queued: drop silently
                return
            if expired:
                await self._log(hook.id, row.event, row.delivery_id, row.attempts, None, False, "expired", 0)
                await self._finish(row, (False, "expired"))
                return
            try:
                secret = self.box.decrypt(hook.secret_enc)
            except Exception:
                self.stats["errors"] += 1
                log.error("webhooks.secret_unreadable", webhook=str(hook.id))
                await self._log(
                    hook.id, row.event, row.delivery_id, row.attempts, None, False, "secret_unreadable", 0
                )
                await self._finish(row, (False, "secret_unreadable"))
                return
            status, ok, error = await self._post(
                hook, row.event, row.delivery_id, row.body.encode(), secret, attempt=row.attempts
            )
            last = status if status is not None else error
            if ok or _permanent(status) or row.attempts > len(RETRY_DELAYS):
                await self._finish(row, (ok, last))
            else:
                delay = RETRY_DELAYS[row.attempts - 1]
                async with self.sessionmaker() as s:
                    await s.execute(
                        update(WebhookOutbox)
                        .where(WebhookOutbox.id == row.id)
                        .values(next_attempt_at=self.now() + timedelta(seconds=delay))
                    )
                    await s.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The lease expires and the row is retried; never let one row kill the batch.
            self.stats["errors"] += 1
            log.exception("webhooks.attempt_failed", webhook=str(hook.id), delivery=row.delivery_id)

    async def _finish(self, row: WebhookOutbox, result: tuple[bool, Any] | None) -> None:
        async with self.sessionmaker() as s:
            await s.execute(delete(WebhookOutbox).where(WebhookOutbox.id == row.id))
            await s.commit()
        if result is not None:
            await self._record(row.webhook_id, *result)

    async def _client_for_attempts(self) -> httpx.AsyncClient:
        """One shared client (connection reuse); rebuilt if ``client_factory`` is replaced."""
        if self._client is None or self._client_from is not self.client_factory:
            old, self._client = self._client, self.client_factory()
            self._client_from = self.client_factory
            if old is not None:
                await old.aclose()
        return self._client

    async def _post(
        self,
        hook: Webhook,
        event: str,
        delivery_id: str,
        body: bytes,
        secret: str,
        *,
        attempt: int,
        http_timeout: httpx.Timeout | None = None,
    ) -> tuple[int | None, bool, str]:
        t0 = time.monotonic()
        status: int | None = None
        error = ""
        try:
            client = await self._client_for_attempts()
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
                timeout=http_timeout or ATTEMPT_TIMEOUT,
            )
            status = resp.status_code
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        ok = status is not None and 200 <= status < 300
        ms = int((time.monotonic() - t0) * 1000)
        WEBHOOK_DELIVERIES.labels("ok" if ok else "failed" if status is not None else "error").inc()
        WEBHOOK_DELIVERY_SECONDS.observe(ms / 1000)
        await self._log(hook.id, event, delivery_id, attempt, status, ok, error, ms)
        return status, ok, error

    # ------------------------------------------------------------------ test ping
    async def send_test(self, hook_id: uuid.UUID, url: str, secret_enc: str) -> bool:
        """One attempt with a short timeout (no retries, nothing queued)."""
        hook = Webhook(id=hook_id, url=url, secret_enc=secret_enc)
        delivery_id = f"dlv_{uuid.uuid4().hex}"
        body = orjson.dumps(
            {
                "id": delivery_id,
                "event": "ping",
                "created_at": self.now().isoformat(),
                "data": {"message": "Hello from Argus Council"},
            }
        )
        try:
            secret = self.box.decrypt(secret_enc)
        except Exception:
            await self._log(hook_id, "ping", delivery_id, 1, None, False, "secret_unreadable", 0)
            await self._record(hook_id, False, "secret_unreadable")
            return False
        status, ok, error = await self._post(
            hook, "ping", delivery_id, body, secret, attempt=1, http_timeout=TEST_TIMEOUT
        )
        await self._record(hook_id, ok, status if status is not None else error)
        return ok

    # ------------------------------------------------------------------ bookkeeping
    @staticmethod
    def _delivery_row(
        hook_id: uuid.UUID,
        event: str,
        delivery_id: str,
        attempt: int,
        status: int | None,
        ok: bool,
        error: str,
        duration_ms: int,
    ) -> WebhookDelivery:
        return WebhookDelivery(
            webhook_id=hook_id,
            ts=datetime.now(UTC),
            event=event,
            delivery_id=delivery_id,
            attempt=attempt,
            status_code=status,
            ok=ok,
            error=error[:300],
            duration_ms=duration_ms,
        )

    async def _log(
        self,
        hook_id: uuid.UUID,
        event: str,
        delivery_id: str,
        attempt: int,
        status: int | None,
        ok: bool,
        error: str,
        duration_ms: int,
    ) -> None:
        async with self.sessionmaker() as s:
            s.add(self._delivery_row(hook_id, event, delivery_id, attempt, status, ok, error, duration_ms))
            await s.commit()

    async def _record(self, hook_id: uuid.UUID, ok: bool, last: Any) -> None:
        """Update the hook's status atomically (concurrent deliveries must not lose increments)."""
        values: dict[str, Any] = {
            "last_status": str(last)[:40] if last is not None else "",
            "last_delivery_at": datetime.now(UTC),
            "failure_count": 0 if ok else Webhook.failure_count + 1,
        }
        async with self.sessionmaker() as s:
            await s.execute(update(Webhook).where(Webhook.id == hook_id).values(**values))
            disabled = 0
            if not ok:
                res = await s.execute(
                    update(Webhook)
                    .where(
                        Webhook.id == hook_id,
                        Webhook.enabled.is_(True),
                        Webhook.failure_count >= DISABLE_AFTER_FAILURES,
                    )
                    .values(enabled=False)
                )
                disabled = res.rowcount  # type: ignore[attr-defined]
            await s.commit()
        if disabled:
            log.warning("webhooks.disabled_after_failures", webhook=str(hook_id))

    async def pending_counts(self, hook_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not hook_ids:
            return {}
        async with self.sessionmaker() as s:
            rows = await s.execute(
                select(WebhookOutbox.webhook_id, func.count(WebhookOutbox.id))
                .where(WebhookOutbox.webhook_id.in_(hook_ids))
                .group_by(WebhookOutbox.webhook_id)
            )
            return {hid: int(n) for hid, n in rows.all()}

    # ------------------------------------------------------------------ lifecycle
    async def drain(self, deadline_s: float | None = None) -> None:
        """Wait for queued emits, then deliver everything currently due (not future retries)."""
        deadline = None if deadline_s is None else time.monotonic() + deadline_s

        def left() -> float | None:
            return None if deadline is None else max(deadline - time.monotonic(), 0)

        if self.tasks:
            await asyncio.wait(set(self.tasks), timeout=left())
        while left() is None or left():
            try:
                handled = await asyncio.wait_for(self.process_due(), left())
            except TimeoutError:
                return
            except Exception:
                self.stats["errors"] += 1
                log.exception("webhooks.drain_failed")
                return
            if not handled:
                return

    async def shutdown(self, grace_s: float = 5.0) -> None:
        """Stop the worker and give in-flight work at most ``grace_s`` seconds.

        Anything not finished stays in the outbox and is resumed by the next process.
        """
        self._stopping.set()
        self._wake.set()
        pending = {t for t in (self._worker, *self.tasks) if t is not None and not t.done()}
        if pending:
            _, late = await asyncio.wait(pending, timeout=grace_s)
            for task in late:
                task.cancel()
            if late:
                log.warning("webhooks.shutdown_deadline", abandoned=len(late))
                await asyncio.gather(*late, return_exceptions=True)
        self._worker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def reset_failures(self, hook_id: uuid.UUID) -> None:
        async with self.sessionmaker() as s:
            await s.execute(update(Webhook).where(Webhook.id == hook_id).values(failure_count=0))
            await s.commit()
