"""Observation storage and the signal loop.

Storage runs against SQLite and real PostgreSQL 16, like the other repositories.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import FeatureObservation, SignalRow, Token
from hades.discovery.repository import TokenRepository
from hades.features.engine import FeatureVector, compute_features
from hades.features.series import Observation, SnapshotSeries
from hades.providers.models import DiscoveredToken
from hades.signals.early_momentum import EarlyMomentumStrategy
from hades.signals.models import MarketState, Signal
from hades.signals.repository import Candidate, SignalRepository
from hades.signals.service import SignalService
from hades.tracking.repository import TrackingRepository
from hades.tracking.schedule import TrackingSchedule

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"
OTHER = "CVVryv1MTsz5Vj5nkSmCH4SkDFRbxzoaP9kJDzDGpump"


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


async def make_candidate(session: AsyncSession, mint: str = MINT) -> Candidate:
    await TokenRepository(session).upsert(
        DiscoveredToken(
            token_address=mint,
            source="pumpportal",
            created_at=NOW - timedelta(seconds=60),
            observed_at=NOW - timedelta(seconds=60),
        )
    )
    token = await session.scalar(select(Token).where(Token.token_address == mint))
    assert token is not None
    return Candidate(
        id=token.id,
        token_address=mint,
        created_at=NOW - timedelta(seconds=60),
        last_snapshot_at=NOW,
    )


def vector_at(mint: str, at: datetime) -> FeatureVector:
    series = SnapshotSeries(
        [
            Observation(
                observed_at=at - timedelta(seconds=20), price_sol=1.0, market_cap_sol=100.0
            ),
            Observation(
                observed_at=at - timedelta(seconds=10), price_sol=1.5, market_cap_sol=150.0
            ),
            Observation(observed_at=at, price_sol=2.0, market_cap_sol=200.0),
        ]
    )
    return compute_features(series, token_address=mint, as_of=at)


class TestImmutableObservations:
    async def test_an_observation_is_stored_with_its_version(self, session: AsyncSession) -> None:
        candidate = await make_candidate(session)
        repository = SignalRepository(session)

        observation_id = await repository.record_observation(candidate, vector_at(MINT, NOW))
        assert observation_id is not None

        row = await session.scalar(select(FeatureObservation))
        assert row is not None
        assert row.token_address == MINT
        assert row.feature_version == "1.0.0"
        assert row.features["market_cap_sol"] == 200.0

    async def test_the_same_instant_is_never_stored_twice(self, session: AsyncSession) -> None:
        """A duplicate silently doubles that moment's weight in every statistic.

        Worse than a wasted row: §17's answers would be quietly wrong rather
        than obviously missing.
        """
        candidate = await make_candidate(session)
        repository = SignalRepository(session)

        first = await repository.record_observation(candidate, vector_at(MINT, NOW))
        second = await repository.record_observation(candidate, vector_at(MINT, NOW))

        assert first is not None
        assert second is None
        count = await session.scalar(select(func.count()).select_from(FeatureObservation))
        assert count == 1

    async def test_a_conflict_leaves_the_original_untouched(self, session: AsyncSession) -> None:
        """DO NOTHING, never DO UPDATE. §11 makes this row immutable."""
        candidate = await make_candidate(session)
        repository = SignalRepository(session)
        await repository.record_observation(candidate, vector_at(MINT, NOW))

        tampered = vector_at(MINT, NOW)
        tampered.values["market_cap_sol"] = 999_999.0
        await repository.record_observation(candidate, tampered)

        row = await session.scalar(select(FeatureObservation))
        assert row is not None
        assert row.features["market_cap_sol"] == 200.0

    async def test_different_instants_are_different_rows(self, session: AsyncSession) -> None:
        candidate = await make_candidate(session)
        repository = SignalRepository(session)
        await repository.record_observation(candidate, vector_at(MINT, NOW))

        later = Candidate(
            id=candidate.id,
            token_address=MINT,
            created_at=candidate.created_at,
            last_snapshot_at=NOW + timedelta(seconds=10),
        )
        await repository.record_observation(later, vector_at(MINT, NOW + timedelta(seconds=10)))

        count = await session.scalar(select(func.count()).select_from(FeatureObservation))
        assert count == 2


class TestSignalStorage:
    async def test_a_signal_points_at_its_observation(self, session: AsyncSession) -> None:
        """Not a copy of the vector.

        Immutability lives in feature_observations; duplicating the values here
        would create a second copy that could disagree with the first.
        """
        candidate = await make_candidate(session)
        repository = SignalRepository(session)
        observation_id = await repository.record_observation(candidate, vector_at(MINT, NOW))
        assert observation_id is not None

        signal = Signal(
            token_address=MINT,
            strategy="early_momentum",
            strategy_version="1.0.0",
            created_at=NOW,
        )
        assert await repository.record_signal(candidate, observation_id, signal) is True

        row = await session.scalar(select(SignalRow))
        assert row is not None
        assert row.observation_id == observation_id
        assert row.created_at.replace(tzinfo=UTC) == NOW

    async def test_a_strategy_fires_at_most_once_per_observation(
        self, session: AsyncSession
    ) -> None:
        """A restart mid-pass must not double-count the same signal."""
        candidate = await make_candidate(session)
        repository = SignalRepository(session)
        observation_id = await repository.record_observation(candidate, vector_at(MINT, NOW))
        assert observation_id is not None

        signal = Signal(
            token_address=MINT,
            strategy="early_momentum",
            strategy_version="1.0.0",
            created_at=NOW,
        )
        assert await repository.record_signal(candidate, observation_id, signal) is True
        assert await repository.record_signal(candidate, observation_id, signal) is False

        count = await session.scalar(select(func.count()).select_from(SignalRow))
        assert count == 1


class TestStats:
    async def test_signal_rate_has_a_denominator(self, session: AsyncSession) -> None:
        """§17 asks how many signals there were. That needs how many chances."""
        candidate = await make_candidate(session)
        repository = SignalRepository(session)

        for offset in range(4):
            moment = NOW + timedelta(seconds=offset * 10)
            at = Candidate(
                id=candidate.id,
                token_address=MINT,
                created_at=candidate.created_at,
                last_snapshot_at=moment,
            )
            observation_id = await repository.record_observation(at, vector_at(MINT, moment))
            if offset == 0 and observation_id is not None:
                await repository.record_signal(
                    at,
                    observation_id,
                    Signal(
                        token_address=MINT,
                        strategy="early_momentum",
                        strategy_version="1.0.0",
                        created_at=moment,
                    ),
                )

        stats = await repository.stats(now=NOW + timedelta(seconds=60))
        assert stats.observations_total == 4
        assert stats.signals_total == 1
        assert stats.signal_rate == pytest.approx(0.25)

    async def test_an_empty_database_has_no_rate(self, session: AsyncSession) -> None:
        """None, not 0.0 -- zero signals out of zero chances is undefined."""
        stats = await SignalRepository(session).stats(now=NOW)
        assert stats.observations_total == 0
        assert stats.signal_rate is None


class FakeDatabase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


class AlwaysFires:
    name = "always"
    version = "1.0.0"

    def __init__(self) -> None:
        self.seen: list[MarketState] = []

    async def evaluate(self, market_state: MarketState) -> Signal | None:
        self.seen.append(market_state)
        return Signal(
            token_address=market_state.token_address,
            strategy=self.name,
            strategy_version=self.version,
            created_at=market_state.as_of,
        )


class NeverFires:
    name = "never"
    version = "1.0.0"

    async def evaluate(self, market_state: MarketState) -> Signal | None:
        return None


async def seed_tracked_token(session: AsyncSession, mint: str = MINT) -> None:
    """A tracked token with three snapshots, as tracking would have left it."""
    schedule = TrackingSchedule()
    await TokenRepository(session).upsert(
        DiscoveredToken(
            token_address=mint,
            source="pumpportal",
            created_at=NOW - timedelta(seconds=60),
            observed_at=NOW - timedelta(seconds=60),
        )
    )
    tracking = TrackingRepository(session, schedule)
    await tracking.admit(slots=1, now=NOW - timedelta(seconds=30))

    from hades.providers.models import MarketSnapshot

    for offset, price in ((-20, 1.0), (-10, 1.5), (0, 2.0)):
        due = await tracking.due(limit=1, now=NOW + timedelta(seconds=offset + 1))
        if not due:
            continue
        await tracking.record_snapshot(
            due[0],
            MarketSnapshot(
                token_address=mint,
                source="pumpfun",
                observed_at=NOW + timedelta(seconds=offset),
                received_at=NOW + timedelta(seconds=offset),
                provider_updated_at=NOW + timedelta(seconds=offset),
                price_sol=price,
                market_cap_sol=price * 100,
                liquidity_sol=price * 10,
                real_sol_reserves=int(price * 10 * 1e9),
                is_complete=False,
            ),
            stale_after_seconds=60,
        )


class TestServiceLoop:
    async def test_a_pass_stores_an_observation_and_a_signal(self, session: AsyncSession) -> None:
        await seed_tracked_token(session)
        strategy = AlwaysFires()
        service = SignalService(FakeDatabase(session), strategy)  # type: ignore[arg-type]

        assert await service.run_once() == 1
        assert (await session.scalar(select(func.count()).select_from(FeatureObservation))) == 1
        assert (await session.scalar(select(func.count()).select_from(SignalRow))) == 1

    async def test_every_evaluation_produces_an_observation_even_without_a_signal(
        self, session: AsyncSession
    ) -> None:
        """The observation table is §17's denominator.

        A system that only stored signals could report "12 signals" and have no
        way to say whether that was out of 20 chances or 20,000.
        """
        await seed_tracked_token(session)
        service = SignalService(FakeDatabase(session), NeverFires())  # type: ignore[arg-type]

        assert await service.run_once() == 0
        assert (await session.scalar(select(func.count()).select_from(FeatureObservation))) == 1
        assert (await session.scalar(select(func.count()).select_from(SignalRow))) == 0

    async def test_a_second_pass_over_the_same_snapshot_does_nothing(
        self, session: AsyncSession
    ) -> None:
        await seed_tracked_token(session)
        strategy = AlwaysFires()
        service = SignalService(FakeDatabase(session), strategy)  # type: ignore[arg-type]

        await service.run_once()
        assert await service.run_once() == 0
        assert (await session.scalar(select(func.count()).select_from(FeatureObservation))) == 1
        # The strategy was not even consulted the second time.
        assert len(strategy.seen) == 1

    async def test_the_strategy_sees_the_snapshot_time_not_wall_clock_now(
        self, session: AsyncSession
    ) -> None:
        await seed_tracked_token(session)
        strategy = AlwaysFires()
        service = SignalService(FakeDatabase(session), strategy)  # type: ignore[arg-type]
        await service.run_once()

        assert strategy.seen[0].as_of == NOW

    async def test_a_failing_token_does_not_stop_the_pass(self, session: AsyncSession) -> None:
        """One bad token must not cost the others their evaluation."""

        class Explodes:
            name = "explodes"
            version = "1.0.0"

            async def evaluate(self, market_state: MarketState) -> Signal | None:
                msg = "strategy blew up"
                raise RuntimeError(msg)

        await seed_tracked_token(session)
        service = SignalService(FakeDatabase(session), Explodes())  # type: ignore[arg-type]

        assert await service.run_once() == 0
        assert service.counters.errors == 1

    async def test_the_real_strategy_runs_end_to_end(self, session: AsyncSession) -> None:
        """Not a fake: the actual hypothesis over an actual stored series."""
        await seed_tracked_token(session)
        service = SignalService(FakeDatabase(session), EarlyMomentumStrategy())  # type: ignore[arg-type]

        await service.run_once()
        # Whether it fires is not the assertion -- that depends on thresholds
        # nobody has validated. What must hold is that the observation exists.
        assert (await session.scalar(select(func.count()).select_from(FeatureObservation))) == 1


class TestCandidateSelection:
    async def test_only_tracked_tokens_are_evaluated(self, session: AsyncSession) -> None:
        await TokenRepository(session).upsert(
            DiscoveredToken(
                token_address=OTHER,
                source="pumpportal",
                created_at=NOW,
                observed_at=NOW,
            )
        )
        candidates = await SignalRepository(session).candidates(limit=10)
        assert candidates == []

    async def test_a_token_without_snapshots_is_not_a_candidate(
        self, session: AsyncSession
    ) -> None:
        await TokenRepository(session).upsert(
            DiscoveredToken(
                token_address=MINT,
                source="pumpportal",
                created_at=NOW - timedelta(seconds=10),
                observed_at=NOW - timedelta(seconds=10),
            )
        )
        await TrackingRepository(session, TrackingSchedule()).admit(slots=1, now=NOW)

        assert await SignalRepository(session).candidates(limit=10) == []

    async def test_min_interval_thins_the_dataset(self, session: AsyncSession) -> None:
        """The knob that keeps a homelab rootfs from filling silently.

        ~100k observations a day at ~1 KB each is ~100 MB/day. Running out of
        disk should be a choice, not an outage.
        """
        await seed_tracked_token(session)
        repository = SignalRepository(session)
        candidate = (await repository.candidates(limit=10))[0]
        await repository.record_observation(candidate, vector_at(MINT, candidate.last_snapshot_at))

        # A new snapshot 10s later, with a 60s minimum, is not yet a candidate.
        await session.execute(
            update(Token)
            .where(Token.token_address == MINT)
            .values(last_snapshot_at=NOW + timedelta(seconds=10))
        )
        await session.commit()

        assert await repository.candidates(limit=10, min_interval_seconds=60.0) == []
        assert len(await repository.candidates(limit=10, min_interval_seconds=0.0)) == 1


async def test_observation_ids_are_unique(session: AsyncSession) -> None:
    candidate = await make_candidate(session)
    repository = SignalRepository(session)
    ids = set()
    for offset in range(3):
        moment = NOW + timedelta(seconds=offset * 10)
        at = Candidate(
            id=candidate.id,
            token_address=MINT,
            created_at=candidate.created_at,
            last_snapshot_at=moment,
        )
        observation_id = await repository.record_observation(at, vector_at(MINT, moment))
        assert observation_id is not None
        ids.add(observation_id)
    assert len(ids) == 3
    assert all(isinstance(value, uuid.UUID) for value in ids)
