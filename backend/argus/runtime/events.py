"""Run event envelope, persistence and live fan-out.

Durable events get a strictly increasing per-run ``seq`` and are stored in
``run_events``. High-frequency *ephemeral* events (``thought.delta``,
``tool.call.output``, ``budget.update``) are fanned out live but not stored;
they carry the latest durable ``seq`` and ``"ephemeral": true`` so clients do
not treat them as gaps. Their content is always recoverable from durable
events (``thought.completed`` carries the full text).

Live fan-out uses Redis pub/sub when available so API replicas and workers
share streams; otherwise an in-process broker is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import orjson
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.logging import get_logger
from argus.db.models import Run, RunEvent

log = get_logger(__name__)

EPHEMERAL_TYPES = frozenset({"thought.delta", "tool.call.output", "budget.update", "subcouncil.event"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "halted"})


class Actor(BaseModel):
    kind: Literal["agent", "user", "system", "remote"] = "system"
    member_id: str | None = None
    role: str | None = None
    name: str | None = None
    overseer: bool = False
    protocol: str | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls(kind="system", name="Argus")

    @classmethod
    def user(cls, name: str = "You") -> Actor:
        return cls(kind="user", name=name)


class Envelope(BaseModel):
    v: int = 1
    seq: int
    id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    run_id: str
    council_id: str | None = None
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    type: str
    actor: Actor = Field(default_factory=Actor.system)
    step_id: str | None = None
    causation_id: str | None = None
    ephemeral: bool = False
    data: dict[str, Any] = Field(default_factory=dict)

    def dumps(self) -> bytes:
        return orjson.dumps(self.model_dump(mode="json", exclude_none=True))


class Broker(Protocol):
    async def publish(self, run_id: str, payload: bytes) -> None: ...
    def subscribe(self, run_id: str, ready: asyncio.Event | None = None) -> AsyncIterator[bytes]: ...
    async def close(self) -> None: ...


class MemoryBroker:
    def __init__(self, maxsize: int = 10_000) -> None:
        self._subs: dict[str, set[asyncio.Queue[bytes]]] = defaultdict(set)
        self._maxsize = maxsize

    async def publish(self, run_id: str, payload: bytes) -> None:
        for q in list(self._subs.get(run_id, ())):
            if q.full():
                # Slow consumer: drop oldest; the client resyncs from durable history.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(payload)

    async def subscribe(  # type: ignore[override]
        self, run_id: str, ready: asyncio.Event | None = None
    ) -> AsyncIterator[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(self._maxsize)
        self._subs[run_id].add(q)
        if ready:
            ready.set()
        try:
            while True:
                yield await q.get()
        finally:
            self._subs[run_id].discard(q)
            if not self._subs[run_id]:
                self._subs.pop(run_id, None)

    async def close(self) -> None:
        self._subs.clear()


class RedisBroker:
    """Redis pub/sub fan-out that shares ONE connection per process.

    One reader task owns one ``PubSub``. Each subscriber gets a bounded in-process queue
    (drop-oldest, like ``MemoryBroker``); a run's channel is subscribed when its first viewer
    arrives and unsubscribed when the last one leaves.
    """

    def __init__(self, client: Redis, prefix: str = "argus:run:", maxsize: int = 10_000) -> None:
        self.client = client
        self.prefix = prefix
        self._maxsize = maxsize
        self._subs: dict[str, set[asyncio.Queue[bytes]]] = {}
        self._pubsub: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def publish(self, run_id: str, payload: bytes) -> None:
        await self.client.publish(f"{self.prefix}{run_id}", payload)

    async def subscribe(  # type: ignore[override]
        self, run_id: str, ready: asyncio.Event | None = None
    ) -> AsyncIterator[bytes]:
        channel = f"{self.prefix}{run_id}"
        q: asyncio.Queue[bytes] = asyncio.Queue(self._maxsize)
        async with self._lock:
            if self._pubsub is None:
                self._pubsub = self.client.pubsub()
            if channel not in self._subs:
                await self._pubsub.subscribe(channel)
                self._subs[channel] = set()
            self._subs[channel].add(q)
            if self._reader is None or self._reader.done():
                self._reader = asyncio.create_task(self._read(), name="redis-broker-reader")
        if ready:
            ready.set()
        try:
            while True:
                yield await q.get()
        finally:
            await self._release(channel, q)

    async def _release(self, channel: str, q: asyncio.Queue[bytes]) -> None:
        async with self._lock:
            subs = self._subs.get(channel)
            if subs is None:
                return
            subs.discard(q)
            if subs:
                return
            del self._subs[channel]
            try:
                await asyncio.wait_for(self._pubsub.unsubscribe(channel), 5)
            except Exception as exc:
                log.warning("events.unsubscribe_failed", error=str(exc)[:200])

    async def _read(self) -> None:
        while True:
            try:
                msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # redis-py reconnects and re-subscribes on the next read; back off meanwhile.
                log.warning("events.pubsub_read_failed", error=str(exc)[:200])
                await asyncio.sleep(1.0)
                continue
            if not msg or msg.get("type") != "message":
                continue
            channel = msg["channel"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            for q in list(self._subs.get(channel, ())):
                if q.full():
                    # Slow consumer: drop oldest; the client resyncs from durable history.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                q.put_nowait(msg["data"])

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None


# Waits between attempts to store an event while the database is briefly unavailable.
PERSIST_DELAYS: tuple[float, ...] = (0.05, 0.25, 1.0)


def is_transient_db_error(exc: BaseException) -> bool:
    """Connection loss, failover or pool exhaustion: worth retrying, unlike constraint errors."""
    if isinstance(exc, OperationalError | InterfaceError | PoolTimeoutError | ConnectionError | TimeoutError):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


class EventSink:
    """Single writer for one run's events: assigns seq, persists, publishes.

    Sequence integrity: ``seq`` only advances for an event that was stored, transient database
    errors are retried, and listeners and the broker are fed inside the lock so live subscribers
    see durable events in ``seq`` order. Once a terminal ``run.status`` is stored the sink is
    closed: later durable events (e.g. from a straggling task) are dropped instead of being
    persisted after the run's final status.
    """

    def __init__(
        self,
        run_id: uuid.UUID,
        council_id: uuid.UUID | None,
        sessionmaker: async_sessionmaker,
        broker: Broker,
        start_seq: int = 0,
    ) -> None:
        self.run_id = run_id
        self.council_id = str(council_id) if council_id else None
        self.sessionmaker = sessionmaker
        self.broker = broker
        self.seq = start_seq
        self.closed = False
        self._lock = asyncio.Lock()
        self.listeners: list[Any] = []  # in-process observers (e.g. snapshot state)

    async def emit(
        self,
        type: str,
        data: dict[str, Any] | None = None,
        *,
        actor: Actor | None = None,
        step_id: str | None = None,
        causation_id: str | None = None,
        durable: bool = False,
    ) -> Envelope:
        ephemeral = type in EPHEMERAL_TYPES and not durable
        async with self._lock:
            env = Envelope(
                seq=self.seq if ephemeral or self.closed else self.seq + 1,
                run_id=str(self.run_id),
                council_id=self.council_id,
                type=type,
                actor=actor or Actor.system(),
                step_id=step_id,
                causation_id=causation_id,
                ephemeral=ephemeral,
                data=data or {},
            )
            if self.closed:
                if not ephemeral:
                    log.warning("events.dropped_after_terminal", run_id=str(self.run_id), type=type)
                return env
            if not ephemeral:
                env = await self._persist_with_retry(env)
                self.seq = env.seq
                if type == "run.status" and env.data.get("status") in TERMINAL_STATUSES:
                    self.closed = True
            for listener in self.listeners:
                try:
                    listener(env)
                except Exception as exc:
                    log.warning("events.listener_failed", error=str(exc)[:200])
            try:
                await self.broker.publish(str(self.run_id), env.dumps())
            except Exception as exc:
                log.warning("events.publish_failed", error=str(exc)[:200])
        return env

    async def _persist_with_retry(self, env: Envelope) -> Envelope:
        attempt = 0
        while True:
            try:
                await self._persist(env)
                return env
            except IntegrityError:
                # Either an earlier attempt did commit (the error hit after the commit), or another
                # writer took this seq. Never write the same event twice and never leave a gap.
                stored = await self._stored_seq(env)
                if stored is not None:
                    return env.model_copy(update={"seq": stored})
                if attempt >= len(PERSIST_DELAYS):
                    raise
                env = env.model_copy(update={"seq": await self._max_seq() + 1})
            except Exception as exc:
                if not is_transient_db_error(exc) or attempt >= len(PERSIST_DELAYS):
                    raise
                log.warning("events.persist_retry", run_id=str(self.run_id), error=str(exc)[:200])
                await asyncio.sleep(PERSIST_DELAYS[attempt])
            attempt += 1

    async def _stored_seq(self, env: Envelope) -> int | None:
        async with self.sessionmaker() as session:
            q = select(RunEvent.seq).where(RunEvent.run_id == self.run_id, RunEvent.event_id == env.id)
            return (await session.execute(q)).scalar_one_or_none()

    async def _max_seq(self) -> int:
        async with self.sessionmaker() as session:
            q = select(func.max(RunEvent.seq)).where(RunEvent.run_id == self.run_id)
            return int((await session.execute(q)).scalar_one_or_none() or 0)

    async def _persist(self, env: Envelope) -> None:
        async with self.sessionmaker() as session:
            session.add(
                RunEvent(
                    run_id=self.run_id,
                    seq=env.seq,
                    event_id=env.id,
                    type=env.type,
                    ts=datetime.fromisoformat(env.ts.replace("Z", "+00:00")),
                    payload=env.model_dump(mode="json", exclude_none=True),
                )
            )
            await session.execute(update(Run).where(Run.id == self.run_id).values(last_seq=env.seq))
            await session.commit()


async def load_events(
    sessionmaker: async_sessionmaker,
    run_id: uuid.UUID,
    after_seq: int = 0,
    before_seq: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    q = select(RunEvent.payload).where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
    if before_seq is not None:
        q = q.where(RunEvent.seq < before_seq)
    async with sessionmaker() as session:
        rows = await session.execute(q.order_by(RunEvent.seq).limit(limit))
        return [r[0] for r in rows]


def build_broker(redis_client: Redis | None) -> Broker:
    return RedisBroker(redis_client) if redis_client is not None else MemoryBroker()
