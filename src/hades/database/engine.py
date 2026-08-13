"""Async database engine, session factory and connectivity probing.

PostgreSQL is the source of truth (task.md §12). Nothing critical lives only in
memory, and the process must survive a temporary database outage rather than
requiring a restart.

Note on pool handling
---------------------
``pool_pre_ping`` validates a connection before handing it out, which is what
makes a brief database restart survivable. Hades v1 additionally ran a watchdog
that called ``engine.dispose()`` on the *first* failed health probe; destroying
the pool under load produced "too many clients" cascades and roughly 52 false
recoveries per 300 log lines. There is deliberately no such watchdog here: the
pool heals itself, and a failing probe is reported, not acted upon.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hades.clock import utc_now
from hades.config.settings import Settings
from hades.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """Result of a real connectivity probe. Never a simulated value."""

    connected: bool
    latency_ms: float | None
    error: str | None


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine from settings."""
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_seconds},
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def probe_database(engine: AsyncEngine) -> DatabaseHealth:
    """Run ``SELECT 1`` and report the real outcome with its latency.

    Failures are logged with their exception type and message and returned as a
    structured result. The exception is not re-raised because callers are health
    endpoints that must answer "degraded" rather than 500 — but it is never
    silently discarded (task.md §10).
    """
    started = utc_now()
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        # TRY400 asks for logger.exception here. The traceback is deliberately
        # omitted: this probe runs on every health-check interval, its call
        # stack is identical on every failure, and all the diagnostic signal
        # (connection refused vs. authentication failure vs. timeout) is already
        # in the exception type and message. Attaching a SQLAlchemy traceback
        # every 15 seconds during an outage buries the useful line.
        logger.error(  # noqa: TRY400
            "database_probe_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return DatabaseHealth(connected=False, latency_ms=None, error=error)

    latency_ms = (utc_now() - started).total_seconds() * 1000.0
    return DatabaseHealth(connected=True, latency_ms=round(latency_ms, 3), error=None)


async def get_migration_revision(engine: AsyncEngine) -> str | None:
    """Return the Alembic revision the database is currently at.

    Returns ``None`` when the ``alembic_version`` table does not exist, which
    means migrations have never been applied against this database.
    """
    query = text("SELECT version_num FROM alembic_version LIMIT 1")
    try:
        async with engine.connect() as connection:
            result = await connection.execute(query)
            row = result.scalar_one_or_none()
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logger.warning(
            "migration_revision_unavailable",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return str(row) if row is not None else None
