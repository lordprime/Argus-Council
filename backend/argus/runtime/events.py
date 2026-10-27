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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.logging import get_logger
from argus.db.models import Run, RunEvent

log = get_logger(__name__)

EPHEMERAL_TYPES = frozenset({"thought.delta", "tool.call.output", "budget.update"})
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
    def subscribe(self, run_id: str) -> AsyncIterator[bytes]: ...
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

    async def subscribe(self, run_id: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        q: asyncio.Queue[bytes] = asyncio.Queue(self._maxsize)
        self._subs[run_id].add(q)
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
    def __init__(self, client: Redis, prefix: str = "argus:run:") -> None:
        self.client = client
        self.prefix = prefix

    async def publish(self, run_id: str, payload: bytes) -> None:
        await self.client.publish(f"{self.prefix}{run_id}", payload)

    async def subscribe(self, run_id: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        pubsub = self.client.pubsub()
        await pubsub.subscribe(f"{self.prefix}{run_id}")
        try:
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    yield msg["data"]
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    async def close(self) -> None:
        pass


class EventSink:
    """Single writer for one run's events: assigns seq, persists, publishes."""

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
    ) -> Envelope:
        ephemeral = type in EPHEMERAL_TYPES
        async with self._lock:
            if not ephemeral:
                self.seq += 1
            env = Envelope(
                seq=self.seq,
                run_id=str(self.run_id),
                council_id=self.council_id,
                type=type,
                actor=actor or Actor.system(),
                step_id=step_id,
                causation_id=causation_id,
                ephemeral=ephemeral,
                data=data or {},
            )
            if not ephemeral:
                await self._persist(env)
        for listener in self.listeners:
            listener(env)
        try:
            await self.broker.publish(str(self.run_id), env.dumps())
        except Exception as exc:
            log.warning("events.publish_failed", error=str(exc)[:200])
        return env

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
