"""Shared fixtures.

The API tests run without a database on purpose: the process must behave
correctly *including* when PostgreSQL is unreachable, and a suite that needs a
live database cannot test the unreachable case.

The persistence tests are the opposite — they need the real thing. ``pgserver``
bundles a PostgreSQL binary and runs it from a temp directory, so the storage
layer is verified against the dialect it actually ships on, with no Docker and
nothing installed system-wide.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.config import Settings


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[object | None]:
    """A real, throwaway PostgreSQL, or None if pgserver is unavailable here.

    Session-scoped: starting a server per test would dominate the runtime.
    Yields None rather than failing so the suite still runs where pgserver has
    no wheel — the tests that need it skip with a stated reason instead of
    silently passing.
    """
    try:
        import pgserver
    except ImportError:
        yield None
        return

    # pgserver logs from an atexit handler, after pytest has closed the streams
    # its handler writes to. That produces a "ValueError: I/O operation on
    # closed file" traceback on every run — noise that looks like a failure.
    logging.getLogger("pgserver").addHandler(logging.NullHandler())
    logging.getLogger("pgserver").propagate = False

    directory = Path(tempfile.mkdtemp(prefix="hades-pgtest-"))
    server = pgserver.get_server(directory)  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        server.cleanup()
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def postgres_dsn(postgres_server: object | None) -> str | None:
    """asyncpg DSN for the throwaway server, or None."""
    if postgres_server is None:
        return None
    uri: str = postgres_server.get_uri()  # type: ignore[attr-defined]
    return uri.replace("postgresql://", "postgresql+asyncpg://")


@pytest.fixture
def settings() -> Settings:
    """Settings pointing at a port nothing listens on.

    Deliberate: it makes "database down" the default test condition.
    """
    return Settings(
        environment="test",
        log_level="WARNING",
        database_url="postgresql+asyncpg://hades:hades@127.0.0.1:1/hades",
        database_connect_timeout_seconds=0.5,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
