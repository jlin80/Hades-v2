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
from sqlalchemy import text

from hades.api.app import create_app
from hades.clock import utc_now
from hades.config.settings import Settings
from hades.database.engine import (
    create_engine,
    create_session_factory,
    get_migration_revision,
    probe_database,
)
from hades.discovery.models import DiscoveredToken
from hades.discovery.repository import count_tokens, insert_new_tokens, latest_discovery_at

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]


@pytest.fixture
def live_settings() -> Settings:
    # Discovery stays off: these tests assert on database behaviour, and a
    # background loop inserting real tokens mid-assertion would make them flaky.
    return Settings(database_url=TEST_DATABASE_URL, log_format="console", discovery_enabled=False)


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

    assert revision == "0002", f"expected the tokens revision, database is at {revision!r}"


# --- Idempotency (task.md §13) -----------------------------------------------


def _token(address: str) -> DiscoveredToken:
    return DiscoveredToken(
        token_address=address,
        symbol="TEST",
        name=None,
        pool_address=None,
        first_seen_at=None,
        observed_at=utc_now(),
        provider_name="integration-test",
        raw={"fixture": True},
    )


@pytest.fixture
async def clean_test_tokens(live_settings: Settings):  # type: ignore[no-untyped-def]
    """Remove this test's rows before and after, leaving real data untouched."""
    engine = create_engine(live_settings)
    factory = create_session_factory(engine)

    async def purge() -> None:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM tokens WHERE discovery_provider = 'integration-test'")
            )
            await session.commit()

    await purge()
    try:
        yield factory
    finally:
        await purge()
        await engine.dispose()


async def test_the_same_token_is_never_stored_twice(clean_test_tokens) -> None:  # type: ignore[no-untyped-def]
    """Processing a token repeatedly must not create duplicate rows."""
    address = "5tEsTiDeMpOtEnCy11111111111111111111111111"

    async with clean_test_tokens() as session:
        first = await insert_new_tokens(session, [_token(address)])
    async with clean_test_tokens() as session:
        second = await insert_new_tokens(session, [_token(address)])
        third = await insert_new_tokens(session, [_token(address), _token(address)])

    assert first == 1, "the first insert should create the row"
    assert second == 0, "a repeat insert must create nothing"
    assert third == 0, "duplicates within one batch must also create nothing"

    async with clean_test_tokens() as session:
        rows = await session.execute(
            text("SELECT count(*) FROM tokens WHERE token_address = :a"), {"a": address}
        )
    assert rows.scalar_one() == 1


async def test_rediscovery_does_not_rewrite_discovered_at(clean_test_tokens) -> None:  # type: ignore[no-untyped-def]
    """discovered_at records when WE first saw a token and must never move."""
    address = "6tEsTfIrStSeEn111111111111111111111111111"

    async with clean_test_tokens() as session:
        await insert_new_tokens(session, [_token(address)])
        original = await session.execute(
            text("SELECT discovered_at FROM tokens WHERE token_address = :a"), {"a": address}
        )
        first_seen = original.scalar_one()

    async with clean_test_tokens() as session:
        await insert_new_tokens(session, [_token(address)])
        again = await session.execute(
            text("SELECT discovered_at FROM tokens WHERE token_address = :a"), {"a": address}
        )

    assert again.scalar_one() == first_seen


async def test_counts_and_latest_discovery_come_from_the_database(clean_test_tokens) -> None:  # type: ignore[no-untyped-def]
    """These power /status and must survive a restart, so they are queried."""
    async with clean_test_tokens() as session:
        before = await count_tokens(session)
        await insert_new_tokens(session, [_token("7cOuNtInG1111111111111111111111111111111")])
        after = await count_tokens(session)
        latest = await latest_discovery_at(session)

    assert after == before + 1
    assert latest is not None
    assert latest.tzinfo is not None, "timestamps must come back timezone-aware"


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
    assert body["database"]["migration_revision"] == "0002"
    assert body["database"]["latency_ms"] is not None


def test_status_reports_real_token_counts_from_the_database(live_settings: Settings) -> None:
    """The count is a database query, not a process counter."""
    with TestClient(create_app(live_settings)) as client:
        discovery = client.get("/status").json()["discovery"]

    assert isinstance(discovery["tokens_discovered"], int)
    assert discovery["tokens_discovered"] >= 0
