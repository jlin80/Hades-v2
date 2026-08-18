"""Computing and storing outcomes, and exporting the research dataset.

Two jobs:

* **Label.** Walk observations whose labels are still provisional, recompute
  from the snapshots that have arrived since, and upsert. A label is rewritten
  until it is final — which is the one place in this system where mutation is
  correct, because the row is a running measurement rather than a record of a
  decision.
* **Export.** Join observations to their outcomes to produce spec §16's dataset:
  TOKEN + FEATURE SNAPSHOT AT T0 + FUTURE OUTCOME.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects import postgresql, sqlite

from hades.db.engine import Database
from hades.db.models import FeatureObservation, MarketSnapshot, ObservationOutcome, SignalRow
from hades.features.series import Observation, SnapshotSeries
from hades.outcomes.labels import (
    DEFAULT_BARRIERS,
    BarrierConfig,
    BarrierLabel,
    compute_outcome,
)
from hades.research.analytics import LabelledRecord

logger = logging.getLogger(__name__)


@dataclass
class OutcomeCounters:
    passes: int = 0
    labelled: int = 0
    finalised: int = 0
    skipped: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def as_dict(self) -> dict[str, int]:
        return {
            "passes": self.passes,
            "labelled": self.labelled,
            "finalised": self.finalised,
            "skipped": self.skipped,
        }


class OutcomeService:
    """Labels observations and exports the research dataset."""

    def __init__(
        self,
        database: Database,
        *,
        barriers: Sequence[BarrierConfig] = DEFAULT_BARRIERS,
        batch_size: int = 200,
        pass_interval_seconds: float = 30.0,
    ) -> None:
        self._database = database
        self._barriers = tuple(barriers)
        self._batch_size = batch_size
        self._pass_interval_seconds = pass_interval_seconds
        self.counters = OutcomeCounters()

    @property
    def barriers(self) -> tuple[BarrierConfig, ...]:
        return self._barriers

    async def label_pending(self, *, now: datetime | None = None) -> int:
        """Recompute every provisional label. Returns how many rows were written.

        Selects observations that have no outcome yet *or* whose outcome is not
        final. A final label is never recomputed: it cannot change, and rereading
        it would be work that produces the same answer forever.
        """
        moment = now or datetime.now(tz=UTC)

        # No age cutoff. An earlier version skipped observations older than
        # twice the longest barrier, as an optimisation, and it silently dropped
        # observations sitting exactly on that boundary -- returning zero labels
        # for a series that was perfectly labellable. The "not yet final" filter
        # below already stops finished rows being re-read, which is where the
        # cost actually was.
        async with self._database.session() as session:
            final_for = (
                select(ObservationOutcome.observation_id)
                .where(ObservationOutcome.is_final.is_(True))
                .group_by(ObservationOutcome.observation_id)
                .having(func.count() >= len(self._barriers))
            )
            pending = (
                await session.execute(
                    select(
                        FeatureObservation.id,
                        FeatureObservation.token_id,
                        FeatureObservation.token_address,
                        FeatureObservation.observed_at,
                    )
                    .where(FeatureObservation.id.not_in(final_for))
                    .order_by(FeatureObservation.observed_at)
                    .limit(self._batch_size)
                )
            ).all()

        written = 0
        for observation_id, token_id, address, observed_at in pending:
            series = await self._series_for(token_id)
            if not series:
                self.counters.skipped += 1
                continue

            for config in self._barriers:
                outcome = compute_outcome(
                    series, at=_as_utc(observed_at), config=config, now=moment
                )
                if outcome is None:
                    self.counters.skipped += 1
                    continue
                await self._upsert(observation_id, token_id, address, outcome)
                written += 1
                self.counters.labelled += 1
                if outcome.is_final:
                    self.counters.finalised += 1

        if written:
            logger.info(
                "outcomes_labelled",
                extra={"context": {"observations": len(pending), "rows": written}},
            )
        return written

    async def _upsert(
        self,
        observation_id: uuid.UUID,
        token_id: uuid.UUID,
        address: str,
        outcome: object,
    ) -> None:
        async with self._database.session() as session:
            dialect = session.bind.dialect.name if session.bind else "postgresql"
            insert = postgresql.insert if dialect == "postgresql" else sqlite.insert

            values = {
                "observation_id": observation_id,
                "token_id": token_id,
                "token_address": address,
                "label_config": outcome.config_name,  # type: ignore[attr-defined]
                "label": outcome.label.value,  # type: ignore[attr-defined]
                "barrier_hit_at_seconds": outcome.barrier_hit_at_seconds,  # type: ignore[attr-defined]
                "is_final": outcome.is_final,  # type: ignore[attr-defined]
                "mfe": outcome.mfe,  # type: ignore[attr-defined]
                "mae": outcome.mae,  # type: ignore[attr-defined]
                "mfe_at_seconds": outcome.mfe_at_seconds,  # type: ignore[attr-defined]
                "mae_at_seconds": outcome.mae_at_seconds,  # type: ignore[attr-defined]
                "observations_after": outcome.observations_after,  # type: ignore[attr-defined]
                "computed_at": datetime.now(tz=UTC),
            }
            for name in ("1m", "5m", "15m", "30m", "1h"):
                values[f"return_{name}"] = outcome.returns.get(name)  # type: ignore[attr-defined]

            statement = insert(ObservationOutcome).values(**values)
            # A provisional label is *meant* to be overwritten as the window
            # elapses -- the row is a running measurement, not a decision.
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["observation_id", "label_config"],
                    set_={k: v for k, v in values.items() if k not in ("observation_id",)},
                )
            )
            await session.commit()

    async def _series_for(self, token_id: uuid.UUID) -> SnapshotSeries:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(
                        MarketSnapshot.observed_at,
                        MarketSnapshot.price_sol,
                        MarketSnapshot.market_cap_sol,
                        MarketSnapshot.liquidity_sol,
                    )
                    .where(MarketSnapshot.token_id == token_id)
                    .order_by(MarketSnapshot.observed_at)
                )
            ).all()
        return SnapshotSeries(
            [
                Observation(
                    observed_at=_as_utc(row[0]),
                    price_sol=row[1],
                    market_cap_sol=row[2],
                    liquidity_sol=row[3],
                )
                for row in rows
            ]
        )

    async def dataset(
        self, *, label_config: str, limit: int = 100_000, final_only: bool = True
    ) -> list[LabelledRecord]:
        """Spec §16's dataset: token + features at T0 + future outcome.

        ``final_only`` defaults to True because a provisional label in a research
        dataset is label noise wearing the costume of evidence.
        """
        async with self._database.session() as session:
            signalled = select(SignalRow.observation_id)
            conditions = [ObservationOutcome.label_config == label_config]
            if final_only:
                conditions.append(ObservationOutcome.is_final.is_(True))

            rows = (
                await session.execute(
                    select(
                        FeatureObservation.token_address,
                        FeatureObservation.features,
                        ObservationOutcome.label,
                        ObservationOutcome.mfe,
                        ObservationOutcome.mae,
                        ObservationOutcome.return_1m,
                        ObservationOutcome.return_5m,
                        ObservationOutcome.return_15m,
                        ObservationOutcome.return_30m,
                        ObservationOutcome.return_1h,
                        FeatureObservation.id.in_(signalled),
                    )
                    .join(
                        ObservationOutcome,
                        ObservationOutcome.observation_id == FeatureObservation.id,
                    )
                    .where(*conditions)
                    .order_by(FeatureObservation.observed_at)
                    .limit(limit)
                )
            ).all()

        return [
            LabelledRecord(
                token_address=row[0],
                features=row[1] or {},
                label=BarrierLabel(row[2]),
                mfe=row[3],
                mae=row[4],
                returns={
                    "1m": row[5],
                    "5m": row[6],
                    "15m": row[7],
                    "30m": row[8],
                    "1h": row[9],
                },
                had_signal=bool(row[10]),
            )
            for row in rows
        ]

    async def stats(self) -> dict[str, int]:
        async with self._database.session() as session:
            total = await session.scalar(select(func.count()).select_from(ObservationOutcome)) or 0
            final = (
                await session.scalar(
                    select(func.count())
                    .select_from(ObservationOutcome)
                    .where(ObservationOutcome.is_final.is_(True))
                )
                or 0
            )
            unlabelled = (
                await session.scalar(
                    select(func.count())
                    .select_from(FeatureObservation)
                    .outerjoin(
                        ObservationOutcome,
                        ObservationOutcome.observation_id == FeatureObservation.id,
                    )
                    .where(
                        or_(
                            ObservationOutcome.id.is_(None),
                            ObservationOutcome.is_final.is_(False),
                        )
                    )
                )
                or 0
            )
        return {
            "outcomes_total": total,
            "outcomes_final": final,
            "observations_pending": unlabelled,
        }

    async def signalled_final_count(self) -> int:
        """Observations that both fired a signal and have a final outcome.

        The number that answers "is there enough evidence yet" -- a finalised
        outcome on an observation nobody signalled says nothing about the
        hypothesis, and a signal with no final outcome yet is still pending.
        """
        async with self._database.session() as session:
            signalled = select(SignalRow.observation_id)
            return (
                await session.scalar(
                    select(func.count(func.distinct(ObservationOutcome.observation_id)))
                    .where(ObservationOutcome.is_final.is_(True))
                    .where(ObservationOutcome.observation_id.in_(signalled))
                )
                or 0
            )

    async def run(self) -> None:
        while True:
            self.counters.passes += 1
            await self.label_pending()
            await asyncio.sleep(self._pass_interval_seconds)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
