"""Engine and session lifecycle.

Owns the async engine, hands out sessions, and answers one question the
``/health`` endpoint needs to answer honestly: *is the database actually
reachable right now?* — measured, never assumed.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hades.config import Settings


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """Result of a real round-trip to PostgreSQL."""

    connected: bool
    latency_ms: float | None
    error: str | None = None


class Database:
    """Async engine plus session factory, with an explicit lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._engine: AsyncEngine = create_async_engine(
            str(settings.database_url),
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_pool_max_overflow,
            pool_pre_ping=True,
            connect_args={"timeout": settings.database_connect_timeout_seconds},
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, rolling back on any exception."""
        async with self._sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def check_health(self) -> DatabaseHealth:
        """Round-trip a trivial query and time it.

        Never raises: an unreachable database is a *reported* state, not a
        500. Broad ``except`` is deliberate and the exception is recorded —
        this is the one place the rule bends, and it bends into the payload.
        """
        started = time.perf_counter()
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            return DatabaseHealth(connected=False, latency_ms=None, error=f"{type(exc).__name__}")
        elapsed_ms = (time.perf_counter() - started) * 1000
        return DatabaseHealth(connected=True, latency_ms=round(elapsed_ms, 2))

    async def close(self) -> None:
        await self._engine.dispose()
