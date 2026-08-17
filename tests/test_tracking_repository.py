"""Admission, scheduling and snapshot persistence.

Runs against SQLite and real PostgreSQL 16, like the discovery storage tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import MarketSnapshot as SnapshotRow
from hades.db.models import Token, TokenState
from hades.discovery.repository import TokenRepository
from hades.providers.models import DiscoveredToken, MarketSnapshot
from hades.tracking.repository import TrackingRepository
from hades.tracking.schedule import TrackingSchedule, TrackingTier

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
SCHEDULE = TrackingSchedule()

MINTS = [
    "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump",
    "CVVryv1MTsz5Vj5nkSmCH4SkDFRbxzoaP9kJDzDGpump",
    "3agfEdE7xRNTvuDhSrUrWbX8zLDA5dq1u89iDktMpump",
    "941Gq45AGf6eye2Vn9hXuNNQaDSLNC5znuado3GCpump",
]


@pytest.fixture(params=["sqlite", "postgresql"])
async def session(
    request: pytest.FixtureRequest, postgres_dsn: str | None
) -> AsyncIterator[AsyncSession]:
    if request.param == "postgresql":
        if postgres_dsn is None:
            pytest.skip("pgserver is not installed; cannot verify against real PostgreSQL")
        engine = create_async_engine(postgres_dsn)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture
def tracking(session: AsyncSession) -> TrackingRepository:
    return TrackingRepository(session, SCHEDULE)


async def discover(session: AsyncSession, mint: str, *, age_seconds: float) -> None:
    """Put a token in the database as discovery would have."""
    await TokenRepository(session).upsert(
        DiscoveredToken(
            token_address=mint,
            source="pumpportal",
            created_at=NOW - timedelta(seconds=age_seconds),
            observed_at=NOW - timedelta(seconds=age_seconds),
        )
    )


def snapshot(mint: str, *, observed_at: datetime, **overrides: object) -> MarketSnapshot:
    payload: dict[str, object] = {
        "token_address": mint,
        "source": "pumpfun",
        "observed_at": observed_at,
        "received_at": observed_at,
        "provider_updated_at": observed_at,
        "virtual_sol_reserves": 58_027_910_356,
        "virtual_token_reserves": 554_733_056_434_731,
        "real_sol_reserves": 28_027_910_356,
        "total_supply": 1_000_000_000_000_000,
        "price_sol": 1.046e-7,
        "market_cap_sol": 104.6,
        "liquidity_sol": 28.03,
        "is_complete": False,
    }
    payload |= overrides
    return MarketSnapshot(**payload)


class TestAdmission:
    async def test_admits_up_to_the_slot_count(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        for mint in MINTS:
            await discover(session, mint, age_seconds=10)

        admitted = await tracking.admit(slots=2, now=NOW)
        assert len(admitted) == 2
        assert await tracking.count_tracking() == 2

    async def test_capacity_is_a_hard_limit(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """Measured: tracking everything needs 64x-146x the primary's capacity.

        The limit is therefore the design, not a safety margin.
        """
        for mint in MINTS:
            await discover(session, mint, age_seconds=10)

        await tracking.admit(slots=1, now=NOW)
        assert await tracking.admit(slots=0, now=NOW) == []
        assert await tracking.count_tracking() == 1

    async def test_newest_first(self, session: AsyncSession, tracking: TrackingRepository) -> None:
        """At capacity the younger token is strictly more valuable.

        The research question is about the first minutes, and an older token has
        already spent some of them unobserved.
        """
        await discover(session, MINTS[0], age_seconds=600)
        await discover(session, MINTS[1], age_seconds=5)

        assert await tracking.admit(slots=1, now=NOW) == [MINTS[1]]

    async def test_a_token_without_created_at_is_not_eligible(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """No created_at means no age, so no tier, so no schedule.

        These are the backfill-exhausted tokens from D12; they are excluded
        here rather than admitted and then found unschedulable.
        """
        await TokenRepository(session).upsert(
            DiscoveredToken(token_address=MINTS[0], source="pumpportal")
        )
        assert await tracking.admit(slots=5, now=NOW) == []

    async def test_a_token_past_the_horizon_is_not_admitted(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """It would be retired on its first snapshot, having spent a slot."""
        await discover(session, MINTS[0], age_seconds=SCHEDULE.retire_after_seconds + 60)
        assert await tracking.admit(slots=5, now=NOW) == []

    async def test_first_snapshot_is_due_immediately(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """The first observation of a young token is the most valuable one."""
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)

        assert len(await tracking.due(limit=10, now=NOW)) == 1

    async def test_eligible_waiting_counts_what_we_decline_to_observe(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        for mint in MINTS:
            await discover(session, mint, age_seconds=10)
        await tracking.admit(slots=1, now=NOW)

        assert await tracking.count_eligible_waiting(now=NOW) == 3


class TestDueQueue:
    async def test_only_tokens_past_their_due_time(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=10, now=NOW)
        await tracking.record_snapshot(
            due[0], snapshot(MINTS[0], observed_at=NOW), stale_after_seconds=60
        )

        # EARLY tier: next one is 10s out.
        assert await tracking.due(limit=10, now=NOW + timedelta(seconds=5)) == []
        assert len(await tracking.due(limit=10, now=NOW + timedelta(seconds=11))) == 1

    async def test_most_overdue_first(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """Falling behind must not become falling behind unevenly."""
        await discover(session, MINTS[0], age_seconds=5)
        await discover(session, MINTS[1], age_seconds=5)
        await tracking.admit(slots=2, now=NOW)

        first = (await tracking.due(limit=10, now=NOW))[0]
        await tracking.record_snapshot(
            first, snapshot(first.token_address, observed_at=NOW), stale_after_seconds=60
        )

        due = await tracking.due(limit=10, now=NOW + timedelta(seconds=60))
        # The one never snapshotted is the more overdue of the two.
        assert due[0].token_address != first.token_address


class TestSnapshotPersistence:
    async def test_snapshots_are_appended_not_replaced(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """Two observations a second apart are two facts, not one updated one."""
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)

        for offset in (0, 10, 20):
            due = await tracking.due(limit=1, now=NOW + timedelta(seconds=offset + 1))
            assert due, f"expected a due token at +{offset}s"
            await tracking.record_snapshot(
                due[0],
                snapshot(MINTS[0], observed_at=NOW + timedelta(seconds=offset)),
                stale_after_seconds=60,
            )

        count = await session.scalar(select(func.count()).select_from(SnapshotRow))
        assert count == 3

    async def test_tier_and_age_are_recorded_on_the_snapshot(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """A dataset whose sampling rate cannot be reconstructed cannot support
        velocity features."""
        await discover(session, MINTS[0], age_seconds=400)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=1, now=NOW)

        tier = await tracking.record_snapshot(
            due[0], snapshot(MINTS[0], observed_at=NOW), stale_after_seconds=60
        )
        assert tier is TrackingTier.MEDIUM

        row = await session.scalar(select(SnapshotRow))
        assert row is not None
        assert row.tier == "MEDIUM"
        assert row.token_age_seconds == pytest.approx(400.0)

    async def test_next_snapshot_uses_the_tier_interval(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        await discover(session, MINTS[0], age_seconds=400)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=1, now=NOW)
        await tracking.record_snapshot(
            due[0], snapshot(MINTS[0], observed_at=NOW), stale_after_seconds=60
        )

        token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.next_snapshot_at is not None
        scheduled = token.next_snapshot_at.replace(tzinfo=UTC)
        assert scheduled == NOW + timedelta(seconds=30)

    async def test_a_restart_resumes_the_schedule(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """next_snapshot_at is persisted, so a restart continues a token's
        series instead of restarting its schedule and leaving a hole."""
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=1, now=NOW)
        await tracking.record_snapshot(
            due[0], snapshot(MINTS[0], observed_at=NOW), stale_after_seconds=60
        )

        fresh = TrackingRepository(session, SCHEDULE)
        assert await fresh.due(limit=10, now=NOW + timedelta(seconds=5)) == []
        assert len(await fresh.due(limit=10, now=NOW + timedelta(seconds=15))) == 1

    async def test_stale_data_is_flagged_from_the_providers_own_clock(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=1, now=NOW)

        await tracking.record_snapshot(
            due[0],
            snapshot(
                MINTS[0],
                observed_at=NOW,
                provider_updated_at=NOW - timedelta(seconds=300),
            ),
            stale_after_seconds=60,
        )

        row = await session.scalar(select(SnapshotRow))
        assert row is not None
        assert row.is_stale is True
        assert row.provider_data_age_seconds == pytest.approx(300.0)

    async def test_fresh_data_is_not_flagged(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=1, now=NOW)
        await tracking.record_snapshot(
            due[0], snapshot(MINTS[0], observed_at=NOW), stale_after_seconds=60
        )

        row = await session.scalar(select(SnapshotRow))
        assert row is not None
        assert row.is_stale is False


class TestStateTransitions:
    async def test_graduation_is_recorded_as_migrated(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """The most interesting thing a Pump.fun token can do, and §2 asks for
        the event to be registered."""
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=1, now=NOW)

        await tracking.record_snapshot(
            due[0],
            snapshot(MINTS[0], observed_at=NOW, is_complete=True),
            stale_after_seconds=60,
        )

        token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.state is TokenState.MIGRATED
        assert token.next_snapshot_at is None

    async def test_aging_out_retires_the_token_and_frees_the_slot(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        await discover(session, MINTS[0], age_seconds=SCHEDULE.retire_after_seconds - 5)
        await tracking.admit(slots=1, now=NOW)
        due = await tracking.due(limit=1, now=NOW)

        await tracking.record_snapshot(
            due[0],
            snapshot(MINTS[0], observed_at=NOW + timedelta(seconds=10)),
            stale_after_seconds=60,
        )

        token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.state is TokenState.INACTIVE
        assert token.next_snapshot_at is None
        assert await tracking.count_tracking() == 0

    async def test_retire_overdue_frees_a_slot_nothing_else_would(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """A token whose provider was down for its whole horizon never gets the
        snapshot that would have retired it, and would hold a slot forever."""
        await discover(session, MINTS[0], age_seconds=10)
        await tracking.admit(slots=1, now=NOW)

        later = NOW + timedelta(seconds=SCHEDULE.retire_after_seconds + 60)
        assert await tracking.retire_overdue(now=later) == 1
        assert await tracking.count_tracking() == 0

    async def test_repeated_failures_give_up_on_the_token(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """The slot is the scarce resource. Holding one for a token that cannot
        be observed costs a token that could have been."""
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = (await tracking.due(limit=1, now=NOW))[0]

        assert await tracking.record_failure(due, max_failures=2, retry_after=30, now=NOW) is False
        assert await tracking.count_tracking() == 1

        again = (await tracking.due(limit=1, now=NOW + timedelta(seconds=60)))[0]
        assert await tracking.record_failure(again, max_failures=2, retry_after=30, now=NOW) is True

        token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.state is TokenState.DEAD
        assert await tracking.count_tracking() == 0

    async def test_a_success_clears_the_failure_streak(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        """The counter detects a token that has gone, not every hiccup it had."""
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = (await tracking.due(limit=1, now=NOW))[0]
        await tracking.record_failure(due, max_failures=3, retry_after=0, now=NOW)

        again = (await tracking.due(limit=1, now=NOW + timedelta(seconds=30)))[0]
        assert again.snapshot_failures == 1
        await tracking.record_snapshot(
            again, snapshot(MINTS[0], observed_at=NOW), stale_after_seconds=60
        )

        token = await TokenRepository(session).get(MINTS[0])
        assert token is not None
        assert token.snapshot_failures == 0


class TestStats:
    async def test_empty_reports_zero_and_none(self, tracking: TrackingRepository) -> None:
        stats = await tracking.stats(now=NOW)
        assert stats.tracking_now == 0
        assert stats.snapshots_total == 0
        # Nothing is due, so there is no lag. None, not 0.
        assert stats.oldest_due_seconds is None

    async def test_oldest_due_reports_how_far_behind_we_are(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        await discover(session, MINTS[0], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)

        stats = await tracking.stats(now=NOW + timedelta(seconds=45))
        assert stats.oldest_due_seconds == pytest.approx(45.0, abs=1.0)

    async def test_counts_reflect_the_database(
        self, session: AsyncSession, tracking: TrackingRepository
    ) -> None:
        await discover(session, MINTS[0], age_seconds=5)
        await discover(session, MINTS[1], age_seconds=5)
        await tracking.admit(slots=1, now=NOW)
        due = (await tracking.due(limit=1, now=NOW))[0]
        await tracking.record_snapshot(
            due, snapshot(due.token_address, observed_at=NOW), stale_after_seconds=60
        )

        stats = await tracking.stats(now=NOW)
        assert stats.tracking_now == 1
        assert stats.snapshots_total == 1
        assert stats.snapshots_last_hour == 1
        assert stats.eligible_waiting == 1


async def test_snapshot_rows_carry_full_provenance(
    session: AsyncSession, tracking: TrackingRepository
) -> None:
    """Spec §9: provider_name, observed_at, received_at, stored_at."""
    await discover(session, MINTS[0], age_seconds=5)
    await tracking.admit(slots=1, now=NOW)
    due = (await tracking.due(limit=1, now=NOW))[0]
    await tracking.record_snapshot(due, snapshot(MINTS[0], observed_at=NOW), stale_after_seconds=60)

    row = await session.scalar(select(SnapshotRow))
    assert row is not None
    assert row.provider_name == "pumpfun"
    assert row.observed_at is not None
    assert row.received_at is not None
    assert row.stored_at is not None
    # The raw curve state is the primary record; everything else derives from it.
    assert row.virtual_sol_reserves == 58_027_910_356
    assert row.total_supply == 1_000_000_000_000_000
    # Linked to the token, so a series can be read without a join on address.
    token = await session.scalar(select(Token).where(Token.token_address == MINTS[0]))
    assert token is not None
    assert row.token_id == token.id
