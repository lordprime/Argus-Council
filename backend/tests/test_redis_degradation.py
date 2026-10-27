"""M8: a Redis outage degrades the API instead of failing every authenticated request.

Policy (see argus/cache/store.py):
* rate limits fall back to a per-process counter (fail open to a local limit, never unlimited);
* plain caching (LLM responses, model lists) is skipped;
* refresh-token revocation, stream tickets and idempotency records fail closed with a 503.
"""

from __future__ import annotations

import asyncio
import time

import fakeredis
import fakeredis.aioredis
import pytest

from argus.cache.store import Cache, CacheUnavailable, RedisBackend, build_cache
from argus.core.metrics import REGISTRY


def _degraded(op: str) -> float:
    return REGISTRY.get_sample_value("argus_cache_degraded_total", {"op": op}) or 0.0


def _broken_backend() -> RedisBackend:
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises redis.ConnectionError
    return RedisBackend(fakeredis.aioredis.FakeRedis(server=server))


class _HungBackend(RedisBackend):
    def __init__(self) -> None:
        super().__init__(fakeredis.aioredis.FakeRedis())

    async def ping(self) -> bool:
        await asyncio.sleep(30)
        return True


# ------------------------------------------------------------ cache unit behaviour
async def test_rate_limit_falls_back_to_local_counter():
    cache = Cache(_broken_backend())
    before = _degraded("rate_limit")
    hits = [await cache.hit_rate_limit("p", 3) for _ in range(5)]
    assert hits == [False, False, False, True, True]  # still limited, per process
    assert _degraded("rate_limit") == before + 5


async def test_strict_operations_raise_service_unavailable():
    cache = Cache(_broken_backend())
    with pytest.raises(CacheUnavailable) as info:
        await cache.get(cache.key("x"))
    assert info.value.status_code == 503 and info.value.code == "cache_unavailable"
    with pytest.raises(CacheUnavailable):
        await cache.set(cache.key("x"), 1)
    with pytest.raises(CacheUnavailable):
        await cache.incr_window(cache.key("x"), 10)


async def test_best_effort_operations_degrade_silently():
    cache = Cache(_broken_backend())
    reads, writes = _degraded("cache_read"), _degraded("cache_write")
    assert await cache.get_best_effort(cache.key("x")) is None
    await cache.set_best_effort(cache.key("x"), {"a": 1})
    assert _degraded("cache_read") == reads + 1 and _degraded("cache_write") == writes + 1

    async def compute():
        return [1, 2]

    assert await cache.get_or_set(cache.key("y"), compute) == [1, 2]


async def test_build_cache_sets_socket_timeouts(settings):
    cache = await build_cache("redis://127.0.0.1:1/0", 60, socket_timeout=1.5, connect_timeout=0.1)
    try:
        kwargs = cache.backend.client.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == 1.5 and kwargs["socket_connect_timeout"] == 0.1
    finally:
        await cache.close()


# ------------------------------------------------------------ API under a Redis outage
@pytest.fixture
def redis_down(app):
    original = app.state.cache.backend
    app.state.cache.backend = _broken_backend()
    yield
    app.state.cache.backend = original


async def test_authenticated_requests_still_work(client, auth, redis_down):
    r = await client.get("/api/v1/councils", headers=auth)
    assert r.status_code == 200
    r = await client.post("/api/v1/runs", headers=auth, json={"topic": "no redis", "mode": "chat", "size": 2})
    assert r.status_code == 201, r.text


async def test_login_still_throttled_per_process(client, auth, redis_down):
    codes = []
    for _ in range(12):
        r = await client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": "nope-nope"}
        )
        codes.append(r.status_code)
    assert 429 in codes


async def test_refresh_fails_closed(client, auth, app):
    pw = "correct horse battery staple"
    tokens = (
        await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": pw})
    ).json()
    original = app.state.cache.backend
    app.state.cache.backend = _broken_backend()
    try:
        r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    finally:
        app.state.cache.backend = original
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "cache_unavailable"
    assert r.headers.get("Retry-After")


async def test_idempotent_requests_fail_closed(client, auth, redis_down):
    r = await client.post(
        "/api/v1/runs",
        headers={**auth, "Idempotency-Key": "k-1"},
        json={"topic": "dup?", "mode": "chat", "size": 2},
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "cache_unavailable"


async def test_stream_ticket_fails_closed(client, auth, app):
    run = (
        await client.post("/api/v1/runs", headers=auth, json={"topic": "t", "mode": "chat", "size": 2})
    ).json()
    original = app.state.cache.backend
    app.state.cache.backend = _broken_backend()
    try:
        r = await client.post(f"/api/v1/runs/{run['id']}/stream-ticket", headers=auth)
    finally:
        app.state.cache.backend = original
    assert r.status_code == 503


# ------------------------------------------------------------ readiness
async def test_ready_reports_cache_down(client, redis_down):
    r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": True, "cache": False}


async def test_ready_bounded_by_timeout(client, app):
    app.state.settings.health_check_timeout_s = 0.2
    original = app.state.cache.backend
    app.state.cache.backend = _HungBackend()
    try:
        t0 = time.monotonic()
        r = await client.get("/health/ready")
    finally:
        app.state.cache.backend = original
    assert time.monotonic() - t0 < 2
    assert r.status_code == 503 and r.json()["checks"]["cache"] is False


async def test_ready_reports_sandbox_without_failing(client, app):
    from argus.sandbox.client import RemoteSandbox

    app.state.runs.sandbox = RemoteSandbox("http://127.0.0.1:1", "t")
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["optional"] == {"sandbox": False}
