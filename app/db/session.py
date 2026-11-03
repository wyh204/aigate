"""Async engine + session factory.

``sqlite+aiosqlite:///./aigate.db`` is the zero-setup default. Point
``AIGATE_DATABASE_URL`` at Postgres and nothing else changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite"):
            # The metering writer and admin reads run concurrently; a busy
            # timeout beats an immediate "database is locked" on SQLite.
            self._engine: AsyncEngine = create_async_engine(url, connect_args={"timeout": 30})
        else:
            self._engine = create_async_engine(url, pool_pre_ping=True)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def is_sqlite(self) -> bool:
        return self._engine.dialect.name == "sqlite"

    async def create_all(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def schema_drift(self) -> list[str]:
        """Columns the ORM writes but the live table lacks.

        ``create_all`` adds missing *tables*, never missing *columns*, so a
        database created by an older version keeps its old shape. Left alone
        this surfaces as a 500 on the first usage query, which is the worst
        possible place to discover it — check at startup instead.
        """
        expected = {"requests": {"saved_cost_usd"}}
        drifts: list[str] = []
        async with self._engine.connect() as connection:
            for table, needed in expected.items():
                present = await connection.run_sync(
                    lambda sync_connection, table=table: {
                        column["name"] for column in inspect(sync_connection).get_columns(table)
                    }
                )
                missing = needed - present
                if missing:
                    drifts.append(f"{table}: missing {', '.join(sorted(missing))}")
        return drifts

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session
