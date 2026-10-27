"""Async engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from argus.core.config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        url = settings.database_url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                # Dev/test only: WAL lets readers proceed during writes; wait instead of failing on locks.
                cur.execute("PRAGMA busy_timeout=30000")
                if ":memory:" not in url:
                    cur.execute("PRAGMA journal_mode=WAL")
                cur.close()

        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        from argus.db import models  # noqa: F401  (register tables)
        from argus.db.base import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
