"""The tracking loop: capacity, rate-limit behaviour, and failure handling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import MarketSnapshot as SnapshotRow
from hades.db.models import TokenState
from hades.discovery.repository import TokenRepository
from hades.providers.errors import ProviderRateLimitedError, ProviderUnavailableError
from hades.providers.models import DiscoveredToken, MarketSnapshot
from hades.tracking.schedule import TrackingSchedule
from hades.tracking.service import TrackingService

MINTS = [
    "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump",
    "CVVryv1MTsz5Vj5nkSmCH4SkDFRbxzoaP9kJDzDGpump",
    "3agfEdE7xRNTvuDhSrUrWbX8zLDA5dq1u89iDktMpump",
    "941Gq45AGf6eye2Vn9hXuNNQaDSLNC5znuado3GCpump",
]


class FakeDatabase:
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
    """Records snapshot calls; can fail, or be rate limited, or graduate."""

    def __init__(
        self,
        *,
        fail: bool = False,
        rate_limited: bool = False,
        is_complete: bool = False,
        provider_lag_seconds: float = 0.0,
    ) -> None:
        self.fail = fail
        self.rate_limited = rate_limited
        self.is_complete = is_complete
        self.provider_lag_seconds = provider_lag_seconds
        self.snapshot_calls: list[str] = []

    async def fetch_snapshot(self, token_address: str) -> MarketSnapshot:
        self.snapshot_calls.append(token_address)
        if self.rate_limited:
            raise ProviderRateLimitedError("pumpfun", "429", 0.0)
        if self.fail:
            raise ProviderUnavailableError("pumpfun", "simulated outage")
        observed = datetime.now(tz=UTC)
        return MarketSnapshot(
            token_address=token_address,
            source="pumpfun",
            observed_at=observed,
            received_at=observed,
            provider_updated_at=observed - timedelta(seconds=self.provider_lag_seconds),
            virtual_sol_reserves=58_027_910_356,
            virtual_token_reserves=554_733_056_434_731,
            real_sol_reserves=28_027_910_356,
            total_supply=1_000_000_000_000_000,
            price_sol=1.046e-7,
            market_cap_sol=104.6,
            liquidity_sol=28.03,
            is_complete=self.is_complete,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def database() -> AsyncIterator[FakeDatabase]:
    db = FakeDatabase()
    await db.setup()
    yield db
    await db.dispose()


async def discover(database: FakeDatabase, mint: str, *, age_seconds: float = 5.0) -> None:
    created = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    async with database.session() as session:
        await TokenRepository(session).upsert(
            DiscoveredToken(
                token_address=mint,
                source="pumpportal",
                created_at=created,
                observed_at=created,
            )
        )


def make_service(database: FakeDatabase, pumpfun: FakePumpFun, **kwargs: object) -> TrackingService:
    defaults: dict[str, object] = {
        "schedule": TrackingSchedule(),
        # No sleeping between requests: the spacing is real behaviour but it
        # would only make the suite slow, and it is asserted separately.
        "request_spacing_seconds": 0.0,
    }
    return TrackingService(database, pumpfun=pumpfun, **(defaults | kwargs))  # type: ignore[arg-type]


class TestCapacity:
    async def test_admits_only_up_to_capacity(self, database: FakeDatabase) -> None:
        """Measured: tracking every token needs 64x-146x the primary's capacity.

        So the limit is the design. Two tokens beyond it are not tracked, and
        that is a decision the system makes explicitly.
        """
        for mint in MINTS:
            await discover(database, mint)

        pumpfun = FakePumpFun()
        service = make_service(database, pumpfun, max_concurrent=2)
        assert await service.run_once() == 2
        assert len(pumpfun.snapshot_calls) == 2

    async def test_a_retired_token_frees_its_slot_for_a_new_one(
        self, database: FakeDatabase
    ) -> None:
        schedule = TrackingSchedule(retire_after_seconds=60.0)
        # Already past the horizon, so it retires on the first pass.
        await discover(database, MINTS[0], age_seconds=120)
        await discover(database, MINTS[1], age_seconds=5)

        service = make_service(database, FakePumpFun(), schedule=schedule, max_concurrent=1)
        await service.run_once()

        async with database.session() as session:
            first = await TokenRepository(session).get(MINTS[0])
            second = await TokenRepository(session).get(MINTS[1])
        assert first is not None
        assert second is not None
        # The overdue one never entered tracking; the young one took the slot.
        assert first.state is TokenState.DISCOVERED
        assert second.state is TokenState.TRACKING

    async def test_estimated_rate_is_reported_from_the_actual_capacity(
        self, database: FakeDatabase
    ) -> None:
        """The arithmetic stays visible instead of folded into a magic number."""
        service = make_service(database, FakePumpFun(), max_concurrent=40)
        assert service.estimated_requests_per_second() == pytest.approx(1.22, abs=0.05)


class TestSnapshotting:
    async def test_a_pass_writes_snapshot_rows(self, database: FakeDatabase) -> None:
        await discover(database, MINTS[0])
        service = make_service(database, FakePumpFun(), max_concurrent=1)

        assert await service.run_once() == 1
        async with database.session() as session:
            count = await session.scalar(select(func.count()).select_from(SnapshotRow))
        assert count == 1
        assert service.counters.snapshots_taken == 1

    async def test_batch_size_bounds_one_pass(self, database: FakeDatabase) -> None:
        for mint in MINTS:
            await discover(database, mint)
        pumpfun = FakePumpFun()
        service = make_service(database, pumpfun, max_concurrent=4, batch_size=2)

        assert await service.run_once() == 2
        assert len(pumpfun.snapshot_calls) == 2

    async def test_graduation_is_counted_and_ends_tracking(self, database: FakeDatabase) -> None:
        await discover(database, MINTS[0])
        service = make_service(database, FakePumpFun(is_complete=True), max_concurrent=1)
        await service.run_once()

        assert service.counters.migrated == 1
        async with database.session() as session:
            token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.state is TokenState.MIGRATED

    async def test_stale_data_is_counted_and_flagged(self, database: FakeDatabase) -> None:
        await discover(database, MINTS[0])
        service = make_service(
            database,
            FakePumpFun(provider_lag_seconds=300.0),
            max_concurrent=1,
            stale_after_seconds=60.0,
        )
        await service.run_once()

        assert service.counters.stale_observed == 1
        async with database.session() as session:
            row = await session.scalar(select(SnapshotRow))
        assert row is not None
        assert row.is_stale is True


class TestFailureHandling:
    async def test_a_provider_outage_does_not_stop_the_pass(self, database: FakeDatabase) -> None:
        """The loop must survive: the next pass may well succeed."""
        await discover(database, MINTS[0])
        service = make_service(database, FakePumpFun(fail=True), max_concurrent=1)

        assert await service.run_once() == 0
        assert service.counters.snapshots_failed == 1

    async def test_repeated_failures_release_the_slot(self, database: FakeDatabase) -> None:
        await discover(database, MINTS[0])
        service = make_service(
            database,
            FakePumpFun(fail=True),
            max_concurrent=1,
            max_snapshot_failures=2,
            failure_retry_seconds=0.0,
        )

        await service.run_once()
        await service.run_once()

        assert service.counters.abandoned == 1
        async with database.session() as session:
            token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.state is TokenState.DEAD

    async def test_a_rate_limit_stops_the_pass_immediately(self, database: FakeDatabase) -> None:
        """Do not push harder into a closing door.

        The primary's real limit is unpublished, so the one measured-safe
        response to a 429 is to stop, not to keep the batch going.
        """
        for mint in MINTS:
            await discover(database, mint)
        pumpfun = FakePumpFun(rate_limited=True)
        service = make_service(database, pumpfun, max_concurrent=4, batch_size=4)

        assert await service.run_once() == 0
        # One attempt, then the pass ends -- not four.
        assert len(pumpfun.snapshot_calls) == 1
        assert service.counters.rate_limited == 1

    async def test_a_rate_limit_does_not_kill_the_token(self, database: FakeDatabase) -> None:
        """Being throttled is our problem, not the token's."""
        await discover(database, MINTS[0])
        pumpfun = FakePumpFun(rate_limited=True)
        service = make_service(database, pumpfun, max_concurrent=1, max_snapshot_failures=1)

        await service.run_once()
        async with database.session() as session:
            token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.state is TokenState.TRACKING
        assert token.snapshot_failures == 0


async def test_repeated_passes_build_a_series(database: FakeDatabase) -> None:
    """The point of the whole phase: many observations of one token over time."""
    schedule = TrackingSchedule(early_interval_seconds=0.0)
    await discover(database, MINTS[0])
    service = make_service(database, FakePumpFun(), schedule=schedule, max_concurrent=1)

    for _ in range(5):
        await service.run_once()

    async with database.session() as session:
        count = await session.scalar(select(func.count()).select_from(SnapshotRow))
    assert count == 5
