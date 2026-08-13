"""End-to-end checks against a real PostgreSQL instance.

Skipped unless TEST_DATABASE_URL points at a live database, so the default
suite stays runnable on a workstation with nothing installed. These are the
tests that actually prove the stack works; the rest prove the logic is right.

Run them against the compose stack with:
    TEST_DATABASE_URL=postgresql+asyncpg://hades:...@127.0.0.1:5432/hades pytest -m integration
"""

import os

import pytest
from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.config.settings import Settings
from hades.database.engine import create_engine, get_migration_revision, probe_database

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]


@pytest.fixture
def live_settings() -> Settings:
    return Settings(database_url=TEST_DATABASE_URL, log_format="console")


async def test_probe_reports_a_real_connection(live_settings: Settings) -> None:
    engine = create_engine(live_settings)
    try:
        probe = await probe_database(engine)
    finally:
        await engine.dispose()

    assert probe.connected is True
    assert probe.error is None
    assert probe.latency_ms is not None
    assert probe.latency_ms >= 0


async def test_migrations_have_been_applied(live_settings: Settings) -> None:
    """The alembic_version row is the proof the migration chain ran."""
    engine = create_engine(live_settings)
    try:
        revision = await get_migration_revision(engine)
    finally:
        await engine.dispose()

    assert revision == "0001", f"expected baseline revision, database is at {revision!r}"


def test_health_endpoint_is_healthy_against_a_live_database(live_settings: Settings) -> None:
    with TestClient(create_app(live_settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_status_endpoint_reports_the_live_revision(live_settings: Settings) -> None:
    with TestClient(create_app(live_settings)) as client:
        body = client.get("/status").json()

    assert body["status"] == "healthy"
    assert body["database"]["connected"] is True
    assert body["database"]["migration_revision"] == "0001"
    assert body["database"]["latency_ms"] is not None
