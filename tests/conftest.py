"""Shared fixtures.

The suite runs without a database on purpose: Phase 0's job is to prove the
process behaves correctly *including* when PostgreSQL is unreachable, and a
suite that needs a live database cannot test the unreachable case.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.config import Settings


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
