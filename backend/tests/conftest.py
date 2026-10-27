from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from argus.core.config import Settings
from argus.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        env="test",
        # CI sets these to run the same suite against real Postgres and Redis.
        database_url=os.environ.get("ARGUS_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/argus.db"),
        redis_url=os.environ.get("ARGUS_TEST_REDIS_URL"),
        secret_key="test-secret-key-that-is-long-enough-1234567890",
        rate_limit_per_minute=10_000,
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def app(settings: Settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app
        await _reset_state(app)


async def _reset_state(app) -> None:
    from argus.cache.store import RedisBackend
    from argus.db.base import Base

    async with app.state.db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if isinstance(app.state.cache.backend, RedisBackend):
        await app.state.cache.backend.client.flushdb()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def register_and_login(client: AsyncClient, email: str = "admin@example.com") -> dict[str, str]:
    pw = "correct horse battery staple"
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": pw})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    return await register_and_login(client)
