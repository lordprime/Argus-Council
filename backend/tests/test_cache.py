import asyncio

import fakeredis.aioredis
import pytest

from argus.cache.store import Cache, MemoryBackend, RedisBackend


@pytest.fixture(params=["memory", "redis"])
def cache(request):
    if request.param == "memory":
        return Cache(MemoryBackend(max_items=50))
    return Cache(RedisBackend(fakeredis.aioredis.FakeRedis()))


async def test_set_get_delete(cache):
    k = cache.key("a")
    await cache.set(k, {"x": 1})
    assert await cache.get(k) == {"x": 1}
    await cache.delete(k)
    assert await cache.get(k) is None


async def test_get_or_set_coalesces(cache):
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return [1, 2]

    results = await asyncio.gather(*[cache.get_or_set(cache.key("c"), compute) for _ in range(10)])
    assert all(r == [1, 2] for r in results)
    assert calls == 1


async def test_rate_limit(cache):
    hits = [await cache.hit_rate_limit("p", 3) for _ in range(5)]
    assert hits == [False, False, False, True, True]


async def test_memory_ttl_and_eviction():
    b = MemoryBackend(max_items=3)
    await b.set_raw("e", b"1", ttl=-1)
    assert await b.get_raw("e") is None
    for i in range(10):
        await b.set_raw(str(i), b"v", None)
    assert len(b._data) <= 3


def test_fingerprint_stable():
    assert Cache.fingerprint({"b": 1, "a": 2}) == Cache.fingerprint({"a": 2, "b": 1})
