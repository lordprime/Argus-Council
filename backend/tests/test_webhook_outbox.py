"""M11: webhook deliveries live in a durable outbox, failures are visible and shutdown is bounded."""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from argus.core.metrics import REGISTRY
from argus.db.models import Webhook, WebhookDelivery, WebhookOutbox
from argus.services import webhooks as wh
from argus.services.webhooks import WebhookDispatcher, verify


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class Receiver:
    """A mock endpoint answering with the queued status codes (200 once they run out)."""

    def __init__(self, *statuses: int, hang: bool = False) -> None:
        self.statuses = list(statuses)
        self.seen: list[httpx.Request] = []
        self.hang = hang
        self.entered = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        self.entered.set()
        if self.hang:
            await asyncio.sleep(60)
        return httpx.Response(self.statuses.pop(0) if self.statuses else 200)

    def factory(self):
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _hook(client, auth, events=("*",)) -> dict:
    r = await client.post(
        "/api/v1/webhooks", headers=auth, json={"url": "https://hooks.example.com/x", "events": list(events)}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _me(client, auth) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/auth/me", headers=auth)).json()["id"])


def _dispatcher(app, receiver: Receiver, clock: Clock) -> WebhookDispatcher:
    d = WebhookDispatcher(app.state.webhooks.settings, app.state.db.sessionmaker, now=clock)
    d.client_factory = receiver.factory()
    return d


async def _outbox(app) -> list[WebhookOutbox]:
    async with app.state.db.sessionmaker() as s:
        return list((await s.execute(select(WebhookOutbox).order_by(WebhookOutbox.id))).scalars())


async def _hook_row(app, hook_id) -> Webhook:
    async with app.state.db.sessionmaker() as s:
        return await s.get(Webhook, uuid.UUID(hook_id))


async def _deliveries(app, hook_id) -> list[WebhookDelivery]:
    async with app.state.db.sessionmaker() as s:
        q = select(WebhookDelivery).where(WebhookDelivery.webhook_id == uuid.UUID(hook_id))
        return list((await s.execute(q.order_by(WebhookDelivery.id))).scalars())


@pytest.fixture
async def quiet(app):
    """Stop the app's own worker so each test drives delivery explicitly."""
    await app.state.webhooks.shutdown(1)
    return app


async def test_pending_deliveries_survive_a_restart(client, auth, quiet):
    app = quiet
    hook = await _hook(client, auth)
    owner = await _me(client, auth)
    first = _dispatcher(app, Receiver(), Clock())
    assert await first.enqueue(owner, "run.completed", {"run_id": "r1"}) == 1
    # The process dies before delivering: nothing is lost, the row is in the outbox.
    rows = await _outbox(app)
    assert len(rows) == 1 and rows[0].attempts == 0 and rows[0].event == "run.completed"

    receiver = Receiver()
    second = _dispatcher(app, receiver, Clock())
    assert await second.process_due() == 1
    assert len(receiver.seen) == 1 and await _outbox(app) == []
    req = receiver.seen[0]
    assert req.headers["x-argus-delivery"] == rows[0].delivery_id
    assert verify(hook["secret"], req.content, req.headers["x-argus-signature"])
    assert json.loads(req.content)["data"] == {"run_id": "r1"}
    await second.shutdown(1)


def _deliveries_metric(result: str) -> float:
    return REGISTRY.get_sample_value("argus_webhook_deliveries_total", {"result": result}) or 0.0


async def test_each_delivery_attempt_is_counted_in_metrics(client, auth, quiet):
    app = quiet
    await _hook(client, auth)
    before = {r: _deliveries_metric(r) for r in ("ok", "failed")}
    seconds = REGISTRY.get_sample_value("argus_webhook_delivery_seconds_count", {}) or 0.0
    clock = Clock()
    d = _dispatcher(app, Receiver(503, 200), clock)
    await d.enqueue(await _me(client, auth), "run.failed", {"x": 1})
    assert await d.process_due() == 1  # attempt 1 -> 503
    clock.advance(wh.RETRY_DELAYS[0])
    assert await d.process_due() == 1  # attempt 2 -> 200
    assert _deliveries_metric("failed") == before["failed"] + 1
    assert _deliveries_metric("ok") == before["ok"] + 1
    assert (REGISTRY.get_sample_value("argus_webhook_delivery_seconds_count", {}) or 0.0) == seconds + 2
    await d.shutdown(1)


async def test_retries_follow_the_schedule_with_a_stable_delivery_id(client, auth, quiet):
    app = quiet
    hook = await _hook(client, auth)
    receiver, clock = Receiver(500, 502, 200), Clock()
    d = _dispatcher(app, receiver, clock)
    await d.enqueue(await _me(client, auth), "run.failed", {"x": 1})

    assert await d.process_due() == 1  # attempt 1 -> 500
    assert await d.process_due() == 0  # not due yet
    clock.advance(wh.RETRY_DELAYS[0])
    assert await d.process_due() == 1  # attempt 2 -> 502
    clock.advance(wh.RETRY_DELAYS[1] - 1)
    assert await d.process_due() == 0
    clock.advance(1)
    assert await d.process_due() == 1  # attempt 3 -> 200

    assert len(receiver.seen) == 3 and await _outbox(app) == []
    assert len({r.headers["x-argus-delivery"] for r in receiver.seen}) == 1
    log = await _deliveries(app, hook["id"])
    assert [(x.attempt, x.status_code, x.ok) for x in log] == [
        (1, 500, False),
        (2, 502, False),
        (3, 200, True),
    ]
    row = await _hook_row(app, hook["id"])
    assert row.failure_count == 0 and row.last_status == "200"
    await d.shutdown(1)


async def test_permanent_failure_and_exhausted_retries_count_once(client, auth, quiet):
    app = quiet
    hook = await _hook(client, auth)
    owner = await _me(client, auth)
    clock = Clock()
    d = _dispatcher(app, Receiver(410), clock)
    await d.enqueue(owner, "run.failed", {})
    assert await d.process_due() == 1
    assert await _outbox(app) == []
    assert (await _hook_row(app, hook["id"])).failure_count == 1

    d.client_factory = Receiver(*([503] * 10)).factory()
    await d.enqueue(owner, "run.failed", {})
    for _ in range(len(wh.RETRY_DELAYS) + 1):
        assert await d.process_due() == 1
        clock.advance(max(wh.RETRY_DELAYS))
    assert await d.process_due() == 0 and await _outbox(app) == []
    assert (await _hook_row(app, hook["id"])).failure_count == 2
    await d.shutdown(1)


async def test_failures_are_visible_not_silent(client, auth, quiet):
    app = quiet
    hook = await _hook(client, auth)
    owner = await _me(client, auth)
    async with app.state.db.sessionmaker() as s:
        await s.execute(update(Webhook).values(secret_enc="not-a-valid-token"))
        await s.commit()
    receiver = Receiver()
    d = _dispatcher(app, receiver, Clock())
    await d.enqueue(owner, "run.completed", {})
    assert await d.process_due() == 1  # handled, not raised
    assert receiver.seen == [] and await _outbox(app) == []
    log = await _deliveries(app, hook["id"])
    assert len(log) == 1 and not log[0].ok and log[0].error == "secret_unreadable"
    assert (await _hook_row(app, hook["id"])).failure_count == 1
    assert d.stats["errors"] == 1

    # A fan-out that cannot even reach the database is counted instead of dying unobserved.
    class Broken:
        def __call__(self):
            raise RuntimeError("db down")

    broken = WebhookDispatcher(app.state.webhooks.settings, Broken())
    broken.emit(owner, "run.completed", {})
    await broken.drain()
    assert broken.stats["enqueue_failed"] == 1
    await d.shutdown(1)


async def test_concurrent_failures_are_all_counted(client, auth, quiet):
    app = quiet
    hook = await _hook(client, auth)
    d = _dispatcher(app, Receiver(), Clock())
    await asyncio.gather(*(d._record(uuid.UUID(hook["id"]), False, "500") for _ in range(8)))
    assert (await _hook_row(app, hook["id"])).failure_count == 8
    await d.shutdown(1)


async def test_disable_after_repeated_failures(client, auth, quiet, monkeypatch):
    app = quiet
    hook = await _hook(client, auth)
    monkeypatch.setattr(wh, "DISABLE_AFTER_FAILURES", 2)
    d = _dispatcher(app, Receiver(), Clock())
    await d._record(uuid.UUID(hook["id"]), False, "500")
    assert (await _hook_row(app, hook["id"])).enabled
    await d._record(uuid.UUID(hook["id"]), False, "500")
    assert not (await _hook_row(app, hook["id"])).enabled
    # Disabled hooks get no new deliveries.
    assert await d.enqueue(await _me(client, auth), "run.completed", {}) == 0
    await d.shutdown(1)


async def test_outbox_is_bounded_per_hook(client, auth, quiet, monkeypatch):
    app = quiet
    hook = await _hook(client, auth)
    owner = await _me(client, auth)
    monkeypatch.setattr(wh, "MAX_PENDING_PER_HOOK", 2)
    d = _dispatcher(app, Receiver(), Clock())
    for _ in range(3):
        await d.enqueue(owner, "run.completed", {})
    assert len(await _outbox(app)) == 2
    log = await _deliveries(app, hook["id"])
    assert len(log) == 1 and log[0].error == "outbox_full" and d.stats["dropped"] == 1
    listed = (await client.get("/api/v1/webhooks", headers=auth)).json()["webhooks"]
    assert listed[0]["pending_deliveries"] == 2
    await d.shutdown(1)


async def test_stale_deliveries_expire(client, auth, quiet):
    app = quiet
    hook = await _hook(client, auth)
    receiver, clock = Receiver(), Clock()
    d = _dispatcher(app, receiver, clock)
    await d.enqueue(await _me(client, auth), "run.completed", {})
    clock.advance(wh.MAX_AGE_S + 1)
    assert await d.process_due() == 1
    assert receiver.seen == [] and await _outbox(app) == []
    assert (await _deliveries(app, hook["id"]))[0].error == "expired"
    await d.shutdown(1)


async def test_shutdown_respects_its_deadline_and_keeps_the_delivery(client, auth, quiet):
    app = quiet
    await _hook(client, auth)
    owner = await _me(client, auth)
    hanging, clock = Receiver(hang=True), Clock()
    d = _dispatcher(app, hanging, clock)
    d.start()
    d.emit(owner, "run.completed", {"run_id": "r"})
    await asyncio.wait_for(hanging.entered.wait(), 5)

    t0 = time.monotonic()
    await d.shutdown(0.3)
    assert time.monotonic() - t0 < 2
    # The in-flight attempt was abandoned, not lost: after the lease it is retried by the next process.
    rows = await _outbox(app)
    assert len(rows) == 1

    receiver = Receiver()
    clock.advance(wh.LEASE_S + 1)
    after_restart = _dispatcher(app, receiver, clock)
    assert await after_restart.process_due() == 1
    assert len(receiver.seen) == 1 and await _outbox(app) == []
    await after_restart.shutdown(1)


async def test_worker_delivers_in_the_background(client, auth, app):
    hook = await _hook(client, auth, events=["run.completed"])
    receiver = Receiver()
    app.state.webhooks.client_factory = receiver.factory()
    app.state.webhooks.emit(await _me(client, auth), "run.completed", {"a": 1})
    app.state.webhooks.emit(await _me(client, auth), "run.started", {"a": 1})  # not subscribed
    await asyncio.wait_for(receiver.entered.wait(), 10)
    # The row is removed just after the response is logged; wait for that too.
    for _ in range(500):
        async with app.state.db.sessionmaker() as s:
            pending = (await s.execute(select(func.count(WebhookOutbox.id)))).scalar_one()
        if not pending:
            break
        await asyncio.sleep(0.02)
    assert pending == 0 and len(receiver.seen) == 1
    log = await _deliveries(app, hook["id"])
    assert [(d.event, d.ok) for d in log] == [("run.completed", True)]


async def test_test_endpoint_is_a_single_attempt(client, auth, app):
    hook = await _hook(client, auth)
    receiver = Receiver(500, 200)
    app.state.webhooks.client_factory = receiver.factory()
    r = await client.post(f"/api/v1/webhooks/{hook['id']}/test", headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["failure_count"] == 1 and len(receiver.seen) == 1
    r = await client.post(f"/api/v1/webhooks/{hook['id']}/test", headers=auth)
    assert r.json()["ok"] is True and r.json()["failure_count"] == 0 and len(receiver.seen) == 2
    secret = (await client.post(f"/api/v1/webhooks/{hook['id']}/rotate-secret", headers=auth)).json()[
        "secret"
    ]
    r = await client.post(f"/api/v1/webhooks/{hook['id']}/test", headers=auth)
    req = receiver.seen[-1]
    assert verify(secret, req.content, req.headers["x-argus-signature"])
    assert req.headers["x-argus-event"] == "ping"
    assert await _outbox(app) == []
