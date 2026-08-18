"""The signal loop.

For each tracked token with a snapshot we have not evaluated: load its series,
compute the feature vector at the newest snapshot, store that vector immutably,
and ask the strategy whether its hypothesis holds.

**Every evaluation produces an observation. Only some produce a signal.** That
asymmetry is the point: §17 asks how many signals there were, and the answer is
meaningless without the denominator. The observation table *is* the denominator
— every row is a moment at which a signal could have fired and mostly did not.

Nothing here executes anything. Spec §12: this generates a research signal, and
whether those have positive expectancy after slippage, fees and risk is the
question the system exists to answer, not something it assumes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from hades.db.engine import Database
from hades.db.models import MarketSnapshot as SnapshotRow
from hades.features.engine import FeatureWindows, compute_features
from hades.features.series import Observation, SnapshotSeries
from hades.signals.models import MarketState
from hades.signals.repository import Candidate, SignalRepository
from hades.signals.strategy import Strategy

logger = logging.getLogger(__name__)


@dataclass
class SignalCounters:
    """In-process counters. The database remains the source of truth."""

    passes: int = 0
    evaluated: int = 0
    observations_stored: int = 0
    observations_skipped: int = 0
    signals_emitted: int = 0
    signals_duplicate: int = 0
    errors: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def as_dict(self) -> dict[str, int]:
        return {
            "passes": self.passes,
            "evaluated": self.evaluated,
            "observations_stored": self.observations_stored,
            "observations_skipped": self.observations_skipped,
            "signals_emitted": self.signals_emitted,
            "signals_duplicate": self.signals_duplicate,
            "errors": self.errors,
        }


class SignalService:
    """Evaluates one hypothesis over tracked tokens."""

    def __init__(
        self,
        database: Database,
        strategy: Strategy,
        *,
        windows: FeatureWindows | None = None,
        series_lookback_seconds: float = 300.0,
        batch_size: int = 25,
        pass_interval_seconds: float = 5.0,
        observation_min_interval_seconds: float = 0.0,
    ) -> None:
        self._database = database
        self._strategy = strategy
        self._windows = windows or FeatureWindows()
        self._series_lookback_seconds = series_lookback_seconds
        self._batch_size = batch_size
        self._pass_interval_seconds = pass_interval_seconds
        self._observation_min_interval_seconds = observation_min_interval_seconds
        self.counters = SignalCounters()

    @property
    def strategy_name(self) -> str:
        return self._strategy.name

    @property
    def strategy_version(self) -> str:
        return self._strategy.version

    async def _load_series(self, candidate: Candidate) -> SnapshotSeries:
        """The token's recent snapshots, as feature inputs.

        Bounded by ``series_lookback_seconds`` rather than loading the whole
        history: the widest window is 60s, so a 300s lookback is generous, and
        an unbounded query would grow without limit as a token is tracked.
        """
        since = candidate.last_snapshot_at - timedelta(seconds=self._series_lookback_seconds)
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(
                        SnapshotRow.observed_at,
                        SnapshotRow.token_age_seconds,
                        SnapshotRow.price_sol,
                        SnapshotRow.market_cap_sol,
                        SnapshotRow.liquidity_sol,
                        SnapshotRow.market_cap_usd,
                        SnapshotRow.real_token_reserves,
                        SnapshotRow.reply_count,
                        SnapshotRow.last_trade_at,
                        SnapshotRow.is_complete,
                    )
                    .where(
                        SnapshotRow.token_id == candidate.id,
                        SnapshotRow.observed_at >= since,
                    )
                    .order_by(SnapshotRow.observed_at)
                )
            ).all()

        return SnapshotSeries(
            [
                Observation(
                    observed_at=_as_utc(row[0]),
                    token_age_seconds=row[1],
                    price_sol=row[2],
                    market_cap_sol=row[3],
                    liquidity_sol=row[4],
                    market_cap_usd=row[5],
                    real_token_reserves=row[6],
                    reply_count=row[7],
                    last_trade_at=_as_utc_or_none(row[8]),
                    is_complete=row[9],
                )
                for row in rows
            ]
        )

    async def evaluate(self, candidate: Candidate) -> bool:
        """Observe and evaluate one token. Returns True if a signal fired."""
        series = await self._load_series(candidate)
        if not series:
            return False

        # as_of is the snapshot's own observation time, never wall-clock now.
        # Using now would fold our processing delay into the feature vector's
        # timestamp and make freshness_seconds measure the wrong thing.
        as_of = candidate.last_snapshot_at
        vector = compute_features(
            series,
            token_address=candidate.token_address,
            as_of=as_of,
            windows=self._windows,
        )

        async with self._database.session() as session:
            observation_id = await SignalRepository(session).record_observation(candidate, vector)

        if observation_id is None:
            # This instant already has a vector. Emitting a signal for it again
            # would double its weight in every statistic computed later.
            self.counters.observations_skipped += 1
            return False

        self.counters.observations_stored += 1
        self.counters.evaluated += 1

        market_state = MarketState(
            token_address=candidate.token_address,
            as_of=as_of,
            feature_version=vector.feature_version,
            features=vector.values,
        )
        signal = await self._strategy.evaluate(market_state)
        if signal is None:
            return False

        async with self._database.session() as session:
            recorded = await SignalRepository(session).record_signal(
                candidate, observation_id, signal
            )

        if recorded:
            self.counters.signals_emitted += 1
            logger.info(
                "signal_emitted",
                extra={
                    "context": {
                        "token_address": signal.token_address,
                        "strategy": signal.strategy,
                        "strategy_version": signal.strategy_version,
                        "token_age_seconds": vector.values.get("token_age_seconds"),
                        "market_cap_sol": vector.values.get("market_cap_sol"),
                        # A research signal, never an instruction.
                        "action": "none",
                    }
                },
            )
        else:
            self.counters.signals_duplicate += 1
        return recorded

    async def run_once(self) -> int:
        """One pass. Returns how many signals fired."""
        self.counters.passes += 1
        async with self._database.session() as session:
            candidates = await SignalRepository(session).candidates(
                limit=self._batch_size,
                min_interval_seconds=self._observation_min_interval_seconds,
            )

        fired = 0
        for candidate in candidates:
            try:
                if await self.evaluate(candidate):
                    fired += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One bad token must not stop the pass: the others are fine and
                # the next pass will retry this one.
                self.counters.errors += 1
                logger.exception(
                    "signal_evaluation_failed",
                    extra={
                        "context": {
                            "token_address": candidate.token_address,
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    },
                )
        return fired

    async def run(self) -> None:
        logger.info(
            "signals_started",
            extra={
                "context": {
                    "strategy": self._strategy.name,
                    "strategy_version": self._strategy.version,
                    # Said out loud at startup so nobody reads a signal count as
                    # a profit claim.
                    "note": "research signals only; no orders are ever produced",
                }
            },
        )
        while True:
            await self.run_once()
            await asyncio.sleep(self._pass_interval_seconds)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)
