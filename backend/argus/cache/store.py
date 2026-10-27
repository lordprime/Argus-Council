"""Cache layer: Redis when configured, an in-process TTL store otherwise.

Both backends expose the same async surface so callers never branch on which
is active. Values are JSON-serialised with orjson.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import orjson
from redis.asyncio import Redis

from argus.core.logging import get_logger

log = get_logger(__name__)


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

    def __init__(self, backend: CacheBackend, namespace: str = "argus", default_ttl: int = 300) -> None:
        self.backend = backend
        self.namespace = namespace
        self.default_ttl = default_ttl
        self._inflight: dict[str, asyncio.Future[Any]] = {}

    def key(self, *parts: str) -> str:
        return ":".join((self.namespace, *parts))

    @staticmethod
    def fingerprint(obj: Any) -> str:
        return hashlib.sha256(orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)).hexdigest()

    async def get(self, key: str) -> Any | None:
        raw = await self.backend.get_raw(key)
        return None if raw is None else orjson.loads(raw)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self.backend.set_raw(key, orjson.dumps(value), ttl if ttl is not None else self.default_ttl)

    async def delete(self, *keys: str) -> None:
        await self.backend.delete(*keys)

    async def get_or_set(
        self, key: str, compute: Callable[[], Awaitable[Any]], ttl: int | None = None
    ) -> Any:
        cached = await self.get(key)
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
                await self.set(key, value, ttl)
            fut.set_result(value)
            return value
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so un-awaited futures don't warn
            raise
        finally:
            self._inflight.pop(key, None)

    async def hit_rate_limit(self, principal: str, limit: int, window_seconds: int = 60) -> bool:
        bucket = int(time.time() // window_seconds)
        count = await self.backend.incr_window(self.key("rl", principal, str(bucket)), window_seconds)
        return count > limit

    async def close(self) -> None:
        await self.backend.close()


async def build_cache(redis_url: str | None, default_ttl: int) -> Cache:
    if redis_url:
        client = Redis.from_url(redis_url, decode_responses=False, health_check_interval=30)
        backend: CacheBackend = RedisBackend(client)
        if not await backend.ping():
            log.warning("cache.redis_unreachable_at_startup", url=redis_url.split("@")[-1])
    else:
        backend = MemoryBackend()
    return Cache(backend, default_ttl=default_ttl)
