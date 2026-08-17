"""Idempotency, against real SQL.

Spec §7: discovery must be idempotent and a restart must never produce
duplicates. That is the single most important property in Phase 2, so it is
tested by executing the actual upsert statement rather than by asserting that
the code looks right.

⚠️ These run on **SQLite**, not Postgres — there is no Postgres available on the
development machine (no server, no Docker). ``ON CONFLICT ... DO UPDATE ... WHERE``
has the same semantics in both, and the ORM types are dialect-portable, so the
logic is genuinely exercised. It is *not* proof of Postgres behaviour. Verifying
against a real Postgres is a prerequisite for Phase 3 and is recorded as such in
docs/DECISIONS.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import TokenState
from hades.discovery.repository import TokenRepository, UpsertOutcome
from hades.providers.models import DiscoveredToken

MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"
OTHER_MINT = "CVVryv1MTsz5Vj5nkSmCH4SkDFRbxzoaP9kJDzDGpump"
CREATOR = "8i6qTrvQZ2c66GdPb8CgQh599CAMUGJPWqVWMwbtjGYf"
CREATED = datetime(2026, 8, 17, 13, 38, 54, tzinfo=UTC)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture
def repository(session: AsyncSession) -> TokenRepository:
    return TokenRepository(session)


def ws_sighting(mint: str = MINT) -> DiscoveredToken:
    """What the WebSocket gives us: fast, no creation timestamp."""
    return DiscoveredToken(
        token_address=mint,
        symbol="UNICORN",
        name="The Unicorn",
        creator_address=CREATOR,
        created_at=None,
        source="pumpportal",
        raw_provider_reference="sig-abc",
    )


def api_sighting(mint: str = MINT) -> DiscoveredToken:
    """What polling the primary gives us: authoritative created_at."""
    return DiscoveredToken(
        token_address=mint,
        symbol="UNICORN",
        name="The Unicorn",
        creator_address=CREATOR,
        created_at=CREATED,
        source="pumpfun",
    )


class TestIdempotency:
    async def test_first_sighting_inserts(self, repository: TokenRepository) -> None:
        assert await repository.upsert(ws_sighting()) is UpsertOutcome.INSERTED
        assert await repository.count() == 1

    async def test_same_token_twice_does_not_duplicate(self, repository: TokenRepository) -> None:
        await repository.upsert(ws_sighting())
        assert await repository.upsert(ws_sighting()) is UpsertOutcome.UNCHANGED
        assert await repository.count() == 1

    async def test_twenty_sightings_still_one_row(self, repository: TokenRepository) -> None:
        """A reconnect loop re-delivering the same creation must cost nothing."""
        for _ in range(20):
            await repository.upsert(ws_sighting())
        assert await repository.count() == 1

    async def test_restart_does_not_duplicate(self, repository: TokenRepository) -> None:
        """The scenario the spec names explicitly.

        A restarted process has an empty in-memory 'seen' set, so it re-observes
        everything the sources replay. The unique constraint, not the set, is
        what holds.
        """
        await repository.upsert(ws_sighting())
        await repository.upsert(api_sighting())

        # New process: no memory at all, only the database.
        recovered = await repository.recover_known_addresses()
        assert recovered == {MINT}

        await repository.upsert(ws_sighting())
        await repository.upsert(api_sighting())
        assert await repository.count() == 1

    async def test_distinct_tokens_are_distinct_rows(self, repository: TokenRepository) -> None:
        await repository.upsert(ws_sighting(MINT))
        await repository.upsert(ws_sighting(OTHER_MINT))
        assert await repository.count() == 2


class TestEnrichmentNeverLosesData:
    async def test_api_fills_the_created_at_the_websocket_lacked(
        self, repository: TokenRepository
    ) -> None:
        await repository.upsert(ws_sighting())
        assert await repository.upsert(api_sighting()) is UpsertOutcome.ENRICHED

        token = await repository.get(MINT)
        assert token is not None
        assert token.created_at is not None
        assert token.created_at.replace(tzinfo=UTC) == CREATED

    async def test_websocket_does_not_erase_a_known_created_at(
        self, repository: TokenRepository
    ) -> None:
        """The failure this guards against is silent and permanent.

        The WebSocket has no created_at. A naive upsert would write NULL over the
        authoritative timestamp on every re-delivery, and token_age, the input
        to every early-window feature, would quietly become uncomputable.
        """
        await repository.upsert(api_sighting())
        await repository.upsert(ws_sighting())

        token = await repository.get(MINT)
        assert token is not None
        assert token.created_at is not None
        assert token.created_at.replace(tzinfo=UTC) == CREATED

    async def test_discovered_at_is_the_first_sighting_not_the_latest(
        self, repository: TokenRepository
    ) -> None:
        """Re-observing a token does not make it newer.

        discovered_at is the baseline every latency measurement is taken from;
        letting a later sighting move it forward would make our detection look
        arbitrarily fast.
        """
        await repository.upsert(ws_sighting())
        first = await repository.get(MINT)
        assert first is not None
        original = first.discovered_at

        await repository.upsert(api_sighting())
        again = await repository.get(MINT)
        assert again is not None
        assert again.discovered_at == original

    async def test_source_attribution_stays_with_whoever_saw_it_first(
        self, repository: TokenRepository
    ) -> None:
        await repository.upsert(ws_sighting())
        await repository.upsert(api_sighting())
        token = await repository.get(MINT)
        assert token is not None
        assert token.source == "pumpportal"

    async def test_provider_reference_survives_a_sighting_without_one(
        self, repository: TokenRepository
    ) -> None:
        await repository.upsert(ws_sighting())
        await repository.upsert(api_sighting())
        token = await repository.get(MINT)
        assert token is not None
        assert token.raw_provider_reference == "sig-abc"

    async def test_partial_metadata_is_filled_in_over_time(
        self, repository: TokenRepository
    ) -> None:
        bare = DiscoveredToken(token_address=MINT, source="pumpportal")
        await repository.upsert(bare)
        token = await repository.get(MINT)
        assert token is not None
        assert token.symbol is None

        await repository.upsert(api_sighting())
        token = await repository.get(MINT)
        assert token is not None
        assert token.symbol == "UNICORN"
        assert token.creator_address == CREATOR


class TestStateNeverGoesBackwards:
    async def test_rediscovery_does_not_reset_a_tracking_token(
        self, repository: TokenRepository, session: AsyncSession
    ) -> None:
        """A token being re-announced must not drop out of tracking.

        Phase 3 will move tokens to TRACKING. If a re-delivered creation event
        reset that, the tracker would restart the token's schedule and the
        snapshot series would have a hole in it.
        """
        await repository.upsert(ws_sighting())
        token = await repository.get(MINT)
        assert token is not None
        token.state = TokenState.TRACKING
        await session.commit()

        await repository.upsert(api_sighting())

        token = await repository.get(MINT)
        assert token is not None
        assert token.state is TokenState.TRACKING


class TestStats:
    async def test_empty_database_reports_zero_and_none(self, repository: TokenRepository) -> None:
        stats = await repository.stats()
        assert stats.total == 0
        assert stats.by_state == {}
        assert stats.last_discovered_at is None
        # No token has a created_at, so there is no latency to report. None,
        # not 0 - a 0 would claim instantaneous discovery.
        assert stats.median_discovery_latency_ms is None

    async def test_latency_is_measured_from_the_two_timestamps(
        self, repository: TokenRepository, session: AsyncSession
    ) -> None:
        await repository.upsert(ws_sighting())
        token = await repository.get(MINT)
        assert token is not None
        # Pretend we saw it 2.5s after it was created.
        token.created_at = CREATED
        token.discovered_at = CREATED + timedelta(milliseconds=2500)
        await session.commit()

        stats = await repository.stats()
        assert stats.median_discovery_latency_ms == 2500.0
        assert stats.with_created_at == 1

    async def test_by_state_only_lists_states_that_exist(self, repository: TokenRepository) -> None:
        await repository.upsert(ws_sighting())
        stats = await repository.stats()
        assert stats.by_state == {"DISCOVERED": 1}
        assert "DEAD" not in stats.by_state
