"""Persistence for observations and signals.

Both writes are idempotent by unique constraint rather than by a check-then-act,
for the same reason discovery's is (D10): a restart mid-pass must not be able to
double-count. In this table the cost of a duplicate is worse than a wasted row —
it silently doubles that moment's weight in every statistic §17 computes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from hades.db.models import FeatureObservation, SignalRow, Token, TokenState
from hades.features.engine import FeatureVector
from hades.signals.models import Signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A tracked token with a snapshot we have not yet evaluated."""

    id: uuid.UUID
    token_address: str
    created_at: datetime
    last_snapshot_at: datetime


@dataclass(frozen=True, slots=True)
class SignalStats:
    """Measured signal state, for ``/status``."""

    observations_total: int
    observations_last_hour: int
    signals_total: int
    signals_last_hour: int
    tokens_with_a_signal: int
    signal_rate: float | None
    last_signal_at: datetime | None


class SignalRepository:
    """Reads candidates, writes observations and signals."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _insert(self) -> object:
        dialect = self._session.bind.dialect.name if self._session.bind else "postgresql"
        return postgresql.insert if dialect == "postgresql" else sqlite.insert

    async def candidates(self, *, limit: int, min_interval_seconds: float = 0.0) -> list[Candidate]:
        """Tracked tokens whose newest snapshot has not been evaluated yet.

        ``min_interval_seconds`` thins the dataset. At 40 concurrent tokens on
        the default schedule this produces roughly 100k observations a day, and
        at ~1 KB a vector that is ~100 MB/day — enough to fill a homelab rootfs
        in months. Zero means observe every snapshot, which is the research
        ideal; the knob exists so running out of disk is a choice rather than an
        outage.
        """
        newest_observation = (
            select(func.max(FeatureObservation.observed_at))
            .where(FeatureObservation.token_id == Token.id)
            .correlate(Token)
            .scalar_subquery()
        )

        # SQL does the comparison; Python does the arithmetic. Adding an
        # interval to a column is dialect-specific — SQLite stores datetimes as
        # text, so `column + interval` silently fails to filter rather than
        # erroring, which is how this returned everything on the first run.
        rows = (
            await self._session.execute(
                select(
                    Token.id,
                    Token.token_address,
                    Token.created_at,
                    Token.last_snapshot_at,
                    newest_observation.label("newest_observation"),
                )
                .where(
                    Token.state == TokenState.TRACKING,
                    Token.created_at.is_not(None),
                    Token.last_snapshot_at.is_not(None),
                    (newest_observation.is_(None)) | (Token.last_snapshot_at > newest_observation),
                )
                .order_by(Token.last_snapshot_at.desc())
                .limit(limit)
            )
        ).all()

        candidates: list[Candidate] = []
        for row in rows:
            if row[2] is None or row[3] is None:
                continue
            last_snapshot_at = _as_utc(row[3])
            previous = _as_utc_or_none(row[4])
            if (
                min_interval_seconds > 0
                and previous is not None
                and (last_snapshot_at - previous).total_seconds() < min_interval_seconds
            ):
                continue
            candidates.append(
                Candidate(
                    id=row[0],
                    token_address=row[1],
                    created_at=_as_utc(row[2]),
                    last_snapshot_at=last_snapshot_at,
                )
            )
        return candidates

    async def record_observation(
        self, candidate: Candidate, vector: FeatureVector
    ) -> uuid.UUID | None:
        """Store the immutable vector. Returns None if this instant already has one.

        ``DO NOTHING``, never ``DO UPDATE``: spec §11 makes this row immutable,
        so the only correct response to a conflict is to leave the original
        alone. Returning None rather than the existing id is deliberate — the
        caller should not emit a signal for a moment already evaluated.
        """
        insert = self._insert()
        statement = (
            insert(FeatureObservation)  # type: ignore[operator]
            .values(
                token_id=candidate.id,
                token_address=candidate.token_address,
                observed_at=vector.observed_at,
                feature_version=vector.feature_version,
                features=vector.values,
            )
            .on_conflict_do_nothing(index_elements=["token_id", "observed_at"])
            .returning(FeatureObservation.id)
        )
        row = (await self._session.execute(statement)).first()
        await self._session.commit()
        self._session.expire_all()
        return None if row is None else row[0]

    async def record_signal(
        self, candidate: Candidate, observation_id: uuid.UUID, signal: Signal
    ) -> bool:
        """Store a research signal. Returns False if it was already recorded."""
        insert = self._insert()
        statement = (
            insert(SignalRow)  # type: ignore[operator]
            .values(
                observation_id=observation_id,
                token_id=candidate.id,
                token_address=candidate.token_address,
                strategy=signal.strategy,
                strategy_version=signal.strategy_version,
                created_at=signal.created_at,
                conditions=[condition.as_dict() for condition in signal.conditions],
            )
            .on_conflict_do_nothing(index_elements=["observation_id", "strategy"])
            .returning(SignalRow.id)
        )
        row = (await self._session.execute(statement)).first()
        await self._session.commit()
        self._session.expire_all()
        return row is not None

    async def stats(self, *, now: datetime | None = None) -> SignalStats:
        moment = now or datetime.now(tz=UTC)
        hour_ago = moment - timedelta(hours=1)

        async def count(
            model: type[FeatureObservation] | type[SignalRow], since: datetime | None
        ) -> int:
            statement = select(func.count()).select_from(model)
            if since is not None:
                column = (
                    FeatureObservation.observed_at
                    if model is FeatureObservation
                    else SignalRow.created_at
                )
                statement = statement.where(column > since)
            total: int = await self._session.scalar(statement) or 0
            return total

        observations_total = await count(FeatureObservation, None)
        signals_total = await count(SignalRow, None)
        tokens_with_signal = (
            await self._session.scalar(
                select(func.count(func.distinct(SignalRow.token_id))).select_from(SignalRow)
            )
            or 0
        )
        last_signal_at = await self._session.scalar(select(func.max(SignalRow.created_at)))

        return SignalStats(
            observations_total=observations_total,
            observations_last_hour=await count(FeatureObservation, hour_ago),
            signals_total=signals_total,
            signals_last_hour=await count(SignalRow, hour_ago),
            tokens_with_a_signal=tokens_with_signal,
            # Signals per observation. The denominator matters: §17 asks how
            # many signals there were, which is meaningless without knowing how
            # many chances there were to fire.
            signal_rate=(signals_total / observations_total if observations_total else None),
            last_signal_at=_as_utc_or_none(last_signal_at),
        )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)
