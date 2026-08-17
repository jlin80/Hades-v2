"""The input to every feature: a token's snapshots, in time order.

Deliberately a plain value type rather than an ORM query. Features must be
computable from anything that can produce observations — the database, a test
fixture, a replay — because a feature that can only be computed by hitting
Postgres cannot be checked by hand.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Observation:
    """One snapshot, reduced to the fields features are computed from.

    ``observed_at`` is the real observation time, not a slot on an assumed grid.
    Measured in Phase 3: the achieved interval was ~12.2s against a 10s target,
    so any velocity computed against the *configured* interval would be ~20%
    wrong. Every rate here divides by an actual elapsed time.
    """

    observed_at: datetime
    token_age_seconds: float | None = None
    price_sol: float | None = None
    market_cap_sol: float | None = None
    liquidity_sol: float | None = None
    market_cap_usd: float | None = None
    real_token_reserves: int | None = None
    reply_count: int | None = None
    last_trade_at: datetime | None = None
    is_complete: bool | None = None


class SnapshotSeries:
    """An immutable, time-ordered series of observations for one token."""

    def __init__(self, observations: list[Observation]) -> None:
        self._observations = sorted(observations, key=lambda o: o.observed_at)
        self._times = [o.observed_at for o in self._observations]

    def __len__(self) -> int:
        return len(self._observations)

    def __bool__(self) -> bool:
        return bool(self._observations)

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._observations)

    def up_to(self, moment: datetime) -> SnapshotSeries:
        """Everything observed at or before ``moment``.

        The one operation that makes look-ahead structurally impossible: a
        feature vector is computed from ``series.up_to(t)``, so a later snapshot
        cannot influence it even by accident. Hades V1's hardest-won lesson was
        that a leaking pipeline fails *looking like success* — excellent offline
        metrics from a model that cannot work — so this is enforced rather than
        remembered.
        """
        cut = bisect.bisect_right(self._times, moment)
        return SnapshotSeries(self._observations[:cut])

    def window(self, moment: datetime, seconds: float) -> SnapshotSeries:
        """Observations in ``(moment - seconds, moment]``."""
        if seconds <= 0:
            return SnapshotSeries([])
        lower = moment.timestamp() - seconds
        return SnapshotSeries(
            [
                observation
                for observation in self.up_to(moment).observations
                if observation.observed_at.timestamp() > lower
            ]
        )

    @property
    def first(self) -> Observation | None:
        return self._observations[0] if self._observations else None

    @property
    def last(self) -> Observation | None:
        return self._observations[-1] if self._observations else None

    def span_seconds(self) -> float | None:
        """Elapsed time actually covered, or None with fewer than two points."""
        if len(self._observations) < 2:
            return None
        return (self._times[-1] - self._times[0]).total_seconds()
