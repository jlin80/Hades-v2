"""Storing outcomes and exporting the research dataset."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import (
    FeatureObservation,
    MarketSnapshot,
    ObservationOutcome,
    SignalRow,
    Token,
)
from hades.discovery.repository import TokenRepository
from hades.outcomes.labels import BarrierConfig, BarrierLabel
from hades.outcomes.service import OutcomeService
from hades.providers.models import DiscoveredToken

T0 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"
CONFIG = BarrierConfig(name="tp30_sl20_1h")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class FakeDatabase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


async def seed(
    session: AsyncSession,
    *,
    prices: list[tuple[float, float]],
    features: dict[str, float | None] | None = None,
    with_signal: bool = False,
) -> None:
    """A token, a snapshot series, and one observation at T0."""
    await TokenRepository(session).upsert(
        DiscoveredToken(
            token_address=MINT,
            source="pumpportal",
            created_at=T0 - timedelta(seconds=60),
            observed_at=T0 - timedelta(seconds=60),
        )
    )
    token = await session.scalar(select(Token).where(Token.token_address == MINT))
    assert token is not None

    for offset, price in prices:
        session.add(
            MarketSnapshot(
                token_id=token.id,
                token_address=MINT,
                provider_name="pumpfun",
                observed_at=T0 + timedelta(seconds=offset),
                received_at=T0 + timedelta(seconds=offset),
                tier="EARLY",
                price_sol=price,
                market_cap_sol=price * 100,
                liquidity_sol=10.0,
                is_stale=False,
            )
        )

    observation = FeatureObservation(
        token_id=token.id,
        token_address=MINT,
        observed_at=T0,
        feature_version="1.0.0",
        features=features or {"token_age_seconds": 60.0, "liquidity_sol": 10.0},
    )
    session.add(observation)
    await session.flush()

    if with_signal:
        session.add(
            SignalRow(
                observation_id=observation.id,
                token_id=token.id,
                token_address=MINT,
                strategy="early_momentum",
                strategy_version="1.0.0",
                created_at=T0,
                conditions=[],
            )
        )
    await session.commit()


def service(session: AsyncSession) -> OutcomeService:
    return OutcomeService(FakeDatabase(session), barriers=(CONFIG,))  # type: ignore[arg-type]


class TestLabelling:
    async def test_an_observation_gets_a_stored_label(self, session: AsyncSession) -> None:
        await seed(session, prices=[(0.0, 1.0), (60.0, 1.4)])
        assert await service(session).label_pending(now=T0 + timedelta(hours=2)) == 1

        row = await session.scalar(select(ObservationOutcome))
        assert row is not None
        assert row.label == BarrierLabel.UPPER.value
        assert row.is_final is True
        assert row.mfe == pytest.approx(0.4)

    async def test_outcomes_are_recorded_without_a_signal(self, session: AsyncSession) -> None:
        """Spec §15: regardless of whether a signal fired or a trade was taken.

        The observations we did not act on are the counterfactual; without them
        the dataset describes our trades and cannot judge the hypothesis.
        """
        await seed(session, prices=[(0.0, 1.0), (60.0, 1.4)], with_signal=False)
        await service(session).label_pending(now=T0 + timedelta(hours=2))

        assert (await session.scalar(select(func.count()).select_from(ObservationOutcome))) == 1

    async def test_a_provisional_label_is_rewritten_as_the_window_elapses(
        self, session: AsyncSession
    ) -> None:
        """The one place mutation is correct: a running measurement."""
        await seed(session, prices=[(0.0, 1.0), (60.0, 1.05)])
        engine = service(session)

        await engine.label_pending(now=T0 + timedelta(seconds=120))
        row = await session.scalar(select(ObservationOutcome))
        assert row is not None
        assert row.label == BarrierLabel.UNRESOLVED.value
        assert row.is_final is False

        session.add(
            MarketSnapshot(
                token_id=row.token_id,
                token_address=MINT,
                provider_name="pumpfun",
                observed_at=T0 + timedelta(seconds=300),
                received_at=T0 + timedelta(seconds=300),
                tier="MEDIUM",
                price_sol=1.5,
                is_stale=False,
            )
        )
        await session.commit()

        await engine.label_pending(now=T0 + timedelta(hours=2))
        await session.refresh(row)
        assert row.label == BarrierLabel.UPPER.value
        assert row.is_final is True
        # Still one row: the label was rewritten, not duplicated.
        assert (await session.scalar(select(func.count()).select_from(ObservationOutcome))) == 1

    async def test_a_final_label_is_not_recomputed(self, session: AsyncSession) -> None:
        """It cannot change, so rereading it is work with a known answer."""
        await seed(session, prices=[(0.0, 1.0), (60.0, 1.4)])
        engine = service(session)
        await engine.label_pending(now=T0 + timedelta(hours=2))
        assert await engine.label_pending(now=T0 + timedelta(hours=2)) == 0

    async def test_multiple_configurations_produce_multiple_rows(
        self, session: AsyncSession
    ) -> None:
        await seed(session, prices=[(0.0, 1.0), (60.0, 1.25)])
        engine = OutcomeService(
            FakeDatabase(session),  # type: ignore[arg-type]
            barriers=(
                BarrierConfig("tight", 0.20, 0.10),
                BarrierConfig("wide", 0.50, 0.40),
            ),
        )
        await engine.label_pending(now=T0 + timedelta(hours=2))

        rows = (await session.execute(select(ObservationOutcome))).scalars().all()
        assert {row.label_config for row in rows} == {"tight", "wide"}
        by_config = {row.label_config: row.label for row in rows}
        assert by_config["tight"] == BarrierLabel.UPPER.value
        assert by_config["wide"] == BarrierLabel.TIMEOUT.value


class TestDataset:
    async def test_the_dataset_joins_features_to_outcomes(self, session: AsyncSession) -> None:
        """Spec §16: TOKEN + FEATURE SNAPSHOT AT T0 + FUTURE OUTCOME."""
        await seed(
            session,
            prices=[(0.0, 1.0), (60.0, 1.4)],
            features={"token_age_seconds": 45.0, "liquidity_sol": 12.0},
            with_signal=True,
        )
        engine = service(session)
        await engine.label_pending(now=T0 + timedelta(hours=2))

        records = await engine.dataset(label_config="tp30_sl20_1h")
        assert len(records) == 1
        record = records[0]
        assert record.token_address == MINT
        assert record.feature("token_age_seconds") == 45.0
        assert record.label is BarrierLabel.UPPER
        assert record.had_signal is True

    async def test_provisional_labels_are_excluded_by_default(self, session: AsyncSession) -> None:
        """A provisional label in a research dataset is noise in evidence's costume."""
        await seed(session, prices=[(0.0, 1.0), (60.0, 1.05)])
        engine = service(session)
        await engine.label_pending(now=T0 + timedelta(seconds=120))

        assert await engine.dataset(label_config="tp30_sl20_1h") == []
        assert len(await engine.dataset(label_config="tp30_sl20_1h", final_only=False)) == 1

    async def test_observations_without_a_signal_are_in_the_dataset(
        self, session: AsyncSession
    ) -> None:
        await seed(session, prices=[(0.0, 1.0), (60.0, 1.4)], with_signal=False)
        engine = service(session)
        await engine.label_pending(now=T0 + timedelta(hours=2))

        records = await engine.dataset(label_config="tp30_sl20_1h")
        assert len(records) == 1
        assert records[0].had_signal is False


async def test_stats_report_what_is_still_pending(session: AsyncSession) -> None:
    await seed(session, prices=[(0.0, 1.0), (60.0, 1.05)])
    engine = service(session)

    await engine.label_pending(now=T0 + timedelta(seconds=120))
    stats = await engine.stats()
    assert stats["outcomes_total"] == 1
    assert stats["outcomes_final"] == 0
    assert stats["observations_pending"] == 1

    await engine.label_pending(now=T0 + timedelta(hours=2))
    stats = await engine.stats()
    assert stats["outcomes_final"] == 1
    assert stats["observations_pending"] == 0
