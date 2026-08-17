"""Discovery orchestration: enrichment, failure handling, and the funnel."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.discovery.repository import TokenRepository, UpsertOutcome
from hades.discovery.service import DiscoveryService
from hades.providers.errors import ProviderUnavailableError
from hades.providers.models import DiscoveredToken

MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"
OTHER = "CVVryv1MTsz5Vj5nkSmCH4SkDFRbxzoaP9kJDzDGpump"
CREATED = datetime(2026, 8, 17, 13, 38, 54, tzinfo=UTC)


class FakeDatabase:
    """A Database-shaped object backed by in-memory SQLite."""

    def __init__(self) -> None:
        self._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self._maker: async_sessionmaker[AsyncSession] | None = None

    async def setup(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self._maker = async_sessionmaker(self._engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        assert self._maker is not None
        async with self._maker() as active:
            yield active

    async def dispose(self) -> None:
        await self._engine.dispose()


class FakePumpFun:
    """Stands in for the primary. Records calls, can be told to fail."""

    def __init__(
        self,
        *,
        created_at: datetime | None = CREATED,
        fail: bool = False,
        listing: list[DiscoveredToken] | None = None,
    ) -> None:
        self.created_at = created_at
        self.fail = fail
        self.listing = listing or []
        self.fetch_calls: list[str] = []
        self.list_calls = 0

    async def fetch_token(self, token_address: str) -> DiscoveredToken:
        self.fetch_calls.append(token_address)
        if self.fail:
            raise ProviderUnavailableError("pumpfun", "simulated outage")
        return DiscoveredToken(
            token_address=token_address,
            symbol="COTUS",
            name="Clown",
            created_at=self.created_at,
            source="pumpfun",
        )

    async def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[DiscoveredToken]:
        self.list_calls += 1
        if self.fail:
            raise ProviderUnavailableError("pumpfun", "simulated outage")
        return self.listing

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def database() -> AsyncIterator[FakeDatabase]:
    db = FakeDatabase()
    await db.setup()
    yield db
    await db.dispose()


def ws_token(mint: str = MINT) -> DiscoveredToken:
    return DiscoveredToken(
        token_address=mint, symbol="UNICORN", source="pumpportal", raw_provider_reference="sig"
    )


def make_service(
    database: FakeDatabase, pumpfun: FakePumpFun, **kwargs: object
) -> DiscoveryService:
    return DiscoveryService(database, pumpfun=pumpfun, **kwargs)  # type: ignore[arg-type]


class TestHandleDoesNotBlockOnTheProvider:
    async def test_persisting_a_sighting_makes_no_provider_call(
        self, database: FakeDatabase
    ) -> None:
        """The write path must not contain an HTTP request.

        Found by running it live: fetching created_at inline 404'd 49 times out
        of 51 (the socket announces a mint before pump.fun indexes it) *and* its
        latency landed inside the discovery-latency measurement.
        """
        pumpfun = FakePumpFun()
        service = make_service(database, pumpfun)

        assert await service.handle(ws_token()) is UpsertOutcome.INSERTED
        assert pumpfun.fetch_calls == []

        async with database.session() as session:
            token = await TokenRepository(session).get(MINT)
        assert token is not None
        assert token.created_at is None
        assert token.symbol == "UNICORN"

    async def test_discovered_at_is_the_providers_observation_time(
        self, database: FakeDatabase
    ) -> None:
        """Not the write time.

        Whatever happens between a frame arriving and the row landing — a slow
        pool, a retry — must not be charged to detection latency.
        """
        observed = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
        token = DiscoveredToken(token_address=MINT, source="pumpportal", observed_at=observed)
        service = make_service(database, FakePumpFun())
        await service.handle(token)

        async with database.session() as session:
            stored = await TokenRepository(session).get(MINT)
        assert stored is not None
        assert stored.discovered_at.replace(tzinfo=UTC) == observed


class TestBackfill:
    async def test_backfill_fills_a_missing_created_at(self, database: FakeDatabase) -> None:
        pumpfun = FakePumpFun()
        service = make_service(database, pumpfun)
        await service.handle(ws_token())

        assert await service.backfill_created_at() == 1
        assert pumpfun.fetch_calls == [MINT]

        async with database.session() as session:
            token = await TokenRepository(session).get(MINT)
        assert token is not None
        assert token.created_at is not None
        assert token.created_at.replace(tzinfo=UTC) == CREATED

    async def test_backfill_keeps_the_discovery_source(self, database: FakeDatabase) -> None:
        """Latency belongs to the path that saw it first, not to the backfiller."""
        service = make_service(database, FakePumpFun())
        await service.handle(ws_token())
        await service.backfill_created_at()

        async with database.session() as session:
            token = await TokenRepository(session).get(MINT)
        assert token is not None
        assert token.source == "pumpportal"

    async def test_backfill_skips_tokens_that_already_have_created_at(
        self, database: FakeDatabase
    ) -> None:
        pumpfun = FakePumpFun()
        service = make_service(database, pumpfun)
        await service.handle(
            DiscoveredToken(token_address=MINT, source="pumpfun", created_at=CREATED)
        )

        assert await service.backfill_created_at() == 0
        assert pumpfun.fetch_calls == []

    async def test_a_still_unindexed_token_stays_null_and_is_retried(
        self, database: FakeDatabase
    ) -> None:
        """A 404 here means 'not yet', not 'never'.

        Leaving created_at NULL and trying again next pass is the whole point:
        the token is still ours, and it gets older, and the race resolves.
        """
        pumpfun = FakePumpFun(fail=True)
        service = make_service(database, pumpfun)
        await service.handle(ws_token())

        assert await service.backfill_created_at() == 0
        assert service.counters.backfill_failures == 1

        async with database.session() as session:
            token = await TokenRepository(session).get(MINT)
        assert token is not None
        assert token.created_at is None

        # Next pass tries the same token again rather than forgetting it.
        pumpfun.fail = False
        assert await service.backfill_created_at() == 1

    async def test_provider_with_no_created_at_counts_as_a_failure(
        self, database: FakeDatabase
    ) -> None:
        pumpfun = FakePumpFun(created_at=None)
        service = make_service(database, pumpfun)
        await service.handle(ws_token())

        assert await service.backfill_created_at() == 0
        assert service.counters.backfill_failures == 1

    async def test_backfill_takes_the_longest_waiting_first(self, database: FakeDatabase) -> None:
        """Oldest sighting first: its indexing race has definitely resolved."""
        older = DiscoveredToken(
            token_address=MINT,
            source="pumpportal",
            observed_at=datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
        )
        newer = DiscoveredToken(
            token_address=OTHER,
            source="pumpportal",
            observed_at=datetime(2026, 8, 17, 12, 5, 0, tzinfo=UTC),
        )
        await make_service(database, FakePumpFun()).handle(newer)
        await make_service(database, FakePumpFun()).handle(older)

        pumpfun = FakePumpFun()
        service = make_service(database, pumpfun, backfill_limit=1)
        await service.backfill_created_at()
        assert pumpfun.fetch_calls == [MINT]


class TestPolling:
    async def test_poll_inserts_new_tokens(self, database: FakeDatabase) -> None:
        listing = [
            DiscoveredToken(token_address=MINT, source="pumpfun", created_at=CREATED),
            DiscoveredToken(token_address=OTHER, source="pumpfun", created_at=CREATED),
        ]
        service = make_service(database, FakePumpFun(listing=listing))

        assert await service.poll_once() == 2
        async with database.session() as session:
            assert await TokenRepository(session).count() == 2

    async def test_second_poll_over_the_same_page_inserts_nothing(
        self, database: FakeDatabase
    ) -> None:
        """Polling overlaps with itself constantly; the overlap must be free.

        At ~0.24 creations/s a 50-item page covers ~3 minutes, so a 60s poll
        re-reads most of the previous page every time.
        """
        listing = [DiscoveredToken(token_address=MINT, source="pumpfun", created_at=CREATED)]
        service = make_service(database, FakePumpFun(listing=listing))

        assert await service.poll_once() == 1
        assert await service.poll_once() == 0
        assert service.counters.unchanged == 1

        async with database.session() as session:
            assert await TokenRepository(session).count() == 1

    async def test_provider_outage_does_not_raise(self, database: FakeDatabase) -> None:
        """A poll failure is recorded and survived, not propagated.

        The loop must keep running: the next pass may well succeed, and a
        crashed poller is how V1 lost the ability to backfill.
        """
        service = make_service(database, FakePumpFun(fail=True))
        assert await service.poll_once() == 0
        assert service.counters.provider_errors == 1

    async def test_poll_backfills_what_the_websocket_missed(self, database: FakeDatabase) -> None:
        """The reason there are two sources at all.

        A disconnect loses whatever was created while we were away. The poller
        picks those up, and because both paths share the idempotent upsert, the
        tokens the socket *did* deliver cost nothing to re-see.
        """
        pumpfun = FakePumpFun(
            listing=[
                DiscoveredToken(token_address=MINT, source="pumpfun", created_at=CREATED),
                DiscoveredToken(token_address=OTHER, source="pumpfun", created_at=CREATED),
            ]
        )
        service = make_service(database, pumpfun)

        # Socket delivered one before dropping.
        await service.handle(ws_token(MINT))
        # Poller sweeps up both.
        await service.poll_once()

        async with database.session() as session:
            repository = TokenRepository(session)
            assert await repository.count() == 2
            first = await repository.get(MINT)
            assert first is not None
            # Still attributed to the socket, which genuinely saw it first.
            assert first.source == "pumpportal"


class TestRecovery:
    async def test_recover_loads_known_addresses(self, database: FakeDatabase) -> None:
        service = make_service(database, FakePumpFun())
        await service.handle(ws_token(MINT))
        await service.handle(ws_token(OTHER))

        fresh = make_service(database, FakePumpFun())
        assert await fresh.recover() == 2

    async def test_recover_on_an_empty_database_is_zero_not_an_error(
        self, database: FakeDatabase
    ) -> None:
        service = make_service(database, FakePumpFun())
        assert await service.recover() == 0


class TestCounters:
    async def test_counters_reflect_what_happened(self, database: FakeDatabase) -> None:
        service = make_service(database, FakePumpFun())
        await service.handle(ws_token())
        await service.handle(ws_token())
        await service.backfill_created_at()

        counters = service.counters.as_dict()
        assert counters["inserted"] == 1
        # The second sighting carries nothing the first did not.
        assert counters["unchanged"] == 1
        assert counters["backfill_fetches"] == 1
        assert counters["enriched"] == 1
