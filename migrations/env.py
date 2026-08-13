"""Alembic environment.

The DSN is read from application settings rather than alembic.ini, so the
migration runner and the application can never disagree about which database
they are talking to.
"""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

# Importing hades.database.models registers every ORM table on Base.metadata.
# All models live in that one module precisely so autogenerate can never miss a
# table because someone forgot to add an import here.
import hades.database.models  # noqa: F401 — imported for its side effect
from hades.config.settings import get_settings
from hades.database.base import Base
from hades.database.engine import create_engine
from hades.observability.logging import configure_logging, get_logger

target_metadata = Base.metadata

settings = get_settings()
configure_logging(level=settings.log_level, log_format=settings.log_format)
logger = get_logger("hades.migrations")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine: AsyncEngine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    logger.info("migrations_starting", database=settings.database_url_safe)
    asyncio.run(_run_async_migrations())
    logger.info("migrations_finished")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
