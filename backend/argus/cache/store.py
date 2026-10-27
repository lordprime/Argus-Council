"""Cache layer: Redis when configured, an in-process TTL store otherwise.

Both backends expose the same async surface so callers never branch on which
is active. Values are JSON-serialised with orjson.

Redis outages (M8). Each use of Redis has an explicit failure policy:

* **Rate limits** (``hit_rate_limit``: per-user API limit, login/registration/password throttles,
  export throttles, MCP) fall back to a per-process counter. Limits keep applying, per replica,
  instead of every request failing or every limit disappearing.
* **Plain caching** (``get_best_effort`` / ``set_best_effort`` / ``get_or_set``: LLM responses,
  provider model lists) is skipped: a miss costs latency, never correctness.
* **Security and correctness records fail closed** with ``CacheUnavailable`` (HTTP 503 plus
  ``Retry-After``) through the strict ``get`` / ``set`` / ``delete`` / ``incr_window``:
  refresh-token single-use (``revoked-jti``; failing open would allow refresh-token replay),
  SSE stream tickets (single-use credentials), and idempotency records and locks (failing open
  could create duplicate runs for a retried ``POST``).

Every degraded operation increments ``argus_cache_degraded_total{op}`` and logs a throttled warning.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError

from argus.core.errors import ServiceUnavailable
from argus.core.logging import get_logger
from argus.core.metrics import CACHE_DEGRADED

log = get_logger(__name__)

# Errors that mean "the cache backend is unreachable or unhealthy" (TimeoutError is an OSError).
BACKEND_ERRORS: tuple[type[BaseException], ...] = (RedisError, OSError)


class CacheUnavailable(ServiceUnavailable):
    code = "cache_unavailable"


class CacheBackend(Protocol):
    async def get_raw(self, key: str) -> bytes | None: ...
    async def set_raw(self, key: str, value: bytes, ttl: int | None) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def incr_window(self, key: str, window_seconds: int) -> int: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class MemoryBackend:
    def __init__(self, max_items: int = 10_000) -> None:
        self._data: dict[str, tuple[bytes, float | None]] = {}
        self._max = max_items
        self._lock = asyncio.Lock()

    def _expired(self, exp: float | None) -> bool:
        return exp is not None and exp <= time.monotonic()

    async def get_raw(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None:
            return None
        if self._expired(item[1]):
            self._data.pop(key, None)
            return None
        return item[0]

    async def set_raw(self, key: str, value: bytes, ttl: int | None) -> None:
        async with self._lock:
            if len(self._data) >= self._max:
                self._evict()
            self._data[key] = (value, time.monotonic() + ttl if ttl else None)

    def _evict(self) -> None:
        now = time.monotonic()
        for k in [k for k, (_, e) in self._data.items() if e is not None and e <= now]:
            del self._data[k]
        while len(self._data) >= self._max:
            self._data.pop(next(iter(self._data)))

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self._data.pop(k, None)

    async def incr_window(self, key: str, window_seconds: int) -> int:
        async with self._lock:
            raw = await self.get_raw(key)
            count = int(raw or b"0") + 1
            exp = self._data.get(key, (b"", None))[1] if raw else time.monotonic() + window_seconds
            if len(self._data) >= self._max and key not in self._data:
                self._evict()
            self._data[key] = (str(count).encode(), exp)
            return count

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


class RedisBackend:
    def __init__(self, client: Redis) -> None:
        self.client = client

    async def get_raw(self, key: str) -> bytes | None:
        return await self.client.get(key)

    async def set_raw(self, key: str, value: bytes, ttl: int | None) -> None:
        await self.client.set(key, value, ex=ttl)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self.client.delete(*keys)

    async def incr_window(self, key: str, window_seconds: int) -> int:
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count)

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()


class Cache:
    """Namespaced JSON cache with get-or-compute and stampede protection."""

    # Degradation warnings are logged at most this often per operation (seconds).
    WARN_INTERVAL_S = 30.0

    def __init__(self, backend: CacheBackend, namespace: str = "argus", default_ttl: int = 300) -> None:
        self.backend = backend
        self.namespace = namespace
        self.default_ttl = default_ttl
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        # Per-process stand-in for rate-limit counters while the backend is unreachable.
        self._local = MemoryBackend(max_items=50_000)
        self._warned: dict[str, float] = {}

    def key(self, *parts: str) -> str:
        return ":".join((self.namespace, *parts))

    @staticmethod
    def fingerprint(obj: Any) -> str:
        return hashlib.sha256(orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)).hexdigest()

    def _degraded(self, op: str, exc: BaseException) -> None:
        CACHE_DEGRADED.labels(op).inc()
        now = time.monotonic()
        if now - self._warned.get(op, -self.WARN_INTERVAL_S) >= self.WARN_INTERVAL_S:
            self._warned[op] = now
            log.warning("cache.degraded", op=op, error=f"{type(exc).__name__}: {exc}"[:200])

    def _unavailable(self, op: str, exc: BaseException) -> CacheUnavailable:
        self._degraded(op, exc)
        return CacheUnavailable("A required service is temporarily unavailable; retry shortly")

    # ---------------------------------------------------------------- strict (fail closed)
    async def get(self, key: str) -> Any | None:
        try:
            raw = await self.backend.get_raw(key)
        except BACKEND_ERRORS as exc:
            raise self._unavailable("strict_read", exc) from exc
        return None if raw is None else orjson.loads(raw)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            await self.backend.set_raw(key, orjson.dumps(value), ttl if ttl is not None else self.default_ttl)
        except BACKEND_ERRORS as exc:
            raise self._unavailable("strict_write", exc) from exc

    async def delete(self, *keys: str) -> None:
        try:
            await self.backend.delete(*keys)
        except BACKEND_ERRORS as exc:
            raise self._unavailable("strict_write", exc) from exc

    async def incr_window(self, key: str, window_seconds: int) -> int:
        try:
            return await self.backend.incr_window(key, window_seconds)
        except BACKEND_ERRORS as exc:
            raise self._unavailable("strict_write", exc) from exc

    # ---------------------------------------------------------------- best effort (skip)
    async def get_best_effort(self, key: str) -> Any | None:
        try:
            raw = await self.backend.get_raw(key)
        except BACKEND_ERRORS as exc:
            self._degraded("cache_read", exc)
            return None
        return None if raw is None else orjson.loads(raw)

    async def set_best_effort(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            await self.backend.set_raw(key, orjson.dumps(value), ttl if ttl is not None else self.default_ttl)
        except BACKEND_ERRORS as exc:
            self._degraded("cache_write", exc)

    async def delete_best_effort(self, *keys: str) -> None:
        try:
            await self.backend.delete(*keys)
        except BACKEND_ERRORS as exc:
            self._degraded("cache_write", exc)

    async def get_or_set(
        self, key: str, compute: Callable[[], Awaitable[Any]], ttl: int | None = None
    ) -> Any:
        cached = await self.get_best_effort(key)
        if cached is not None:
            return cached
        # Coalesce concurrent misses for the same key within this process.
        if key in self._inflight:
            return await self._inflight[key]
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await compute()
            if value is not None:
                await self.set_best_effort(key, value, ttl)
            fut.set_result(value)
            return value
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so un-awaited futures don't warn
            raise
        finally:
            self._inflight.pop(key, None)

    # ---------------------------------------------------------------- rate limits (local fallback)
    async def hit_rate_limit(self, principal: str, limit: int, window_seconds: int = 60) -> bool:
        bucket = int(time.time() // window_seconds)
        key = self.key("rl", principal, str(bucket))
        try:
            count = await self.backend.incr_window(key, window_seconds)
        except BACKEND_ERRORS as exc:
            self._degraded("rate_limit", exc)
            count = await self._local.incr_window(key, window_seconds)
        return count > limit

    async def close(self) -> None:
        await self.backend.close()


def redis_client(
    redis_url: str, *, socket_timeout: float | None = None, connect_timeout: float | None = None
) -> Redis:
    return Redis.from_url(
        redis_url,
        decode_responses=False,
        health_check_interval=30,
        socket_timeout=socket_timeout,
        socket_connect_timeout=connect_timeout,
    )


async def build_cache(
    redis_url: str | None,
    default_ttl: int,
    *,
    socket_timeout: float | None = 2.0,
    connect_timeout: float | None = 2.0,
) -> Cache:
    """Build the cache. Redis commands are bounded by ``socket_timeout`` so a hung Redis turns into
    a fast, handled error instead of stalling every request."""
    if redis_url:
        client = redis_client(redis_url, socket_timeout=socket_timeout, connect_timeout=connect_timeout)
        backend: CacheBackend = RedisBackend(client)
        if not await backend.ping():
            log.warning("cache.redis_unreachable_at_startup", url=redis_url.split("@")[-1])
    else:
        backend = MemoryBackend()
    return Cache(backend, default_ttl=default_ttl)
