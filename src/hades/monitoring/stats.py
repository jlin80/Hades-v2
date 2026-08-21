"""Cross-subsystem database aggregates, computed on a clock rather than per request.

``/status`` was measured at **~14 seconds** on CT202. It runs unbounded
``COUNT(*)`` over ``feature_observations`` and ``observation_outcomes`` — 173k
and 520k rows — on every single request, so asking how the system is doing put a
full table scan on the database the collectors were trying to write to. Two
consequences, both bad: nobody could poll it, and polling it made the thing it
was reporting on worse.

That also made a metrics endpoint impossible. Prometheus defaults to a 15-second
scrape interval and a 10-second timeout, so a 14-second endpoint does not merely
scrape slowly, it never succeeds.

So the expensive aggregates move onto a refresh loop and both endpoints read the
last snapshot. **The freshness is reported, not hidden.** ``routes.py`` opens by
saying neither endpoint is cached, because "a cached health check reports the
past, and the past is exactly what you do not want when you are asking whether
something is broken right now" — that principle is kept where it matters:

* Liveness and the in-process counters stay live. ``/health`` still opens a real
  connection on every request, and the loop states still come from the runtimes.
* Only the row-counting aggregates are cached, and every response carries
  ``age_seconds`` so a reader can see exactly how old the number is instead of
  trusting that it is current.

A stale number you can date is a measurement. A stale number presented as live is
the V1 dashboard bug.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from hades.config import Settings
from hades.db.engine import Database
from hades.discovery.repository import DiscoveryStats, TokenRepository
from hades.outcomes.service import OutcomeService
from hades.paper.service import PaperTradingService
from hades.risk.engine import RiskState
from hades.signals.repository import SignalRepository, SignalStats
from hades.tracking.repository import TrackingRepository, TrackingStats
from hades.tracking.runtime import schedule_from_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """Every expensive aggregate, from one moment."""

    computed_at: datetime
    duration_seconds: float
    discovery: DiscoveryStats
    tracking: TrackingStats
    signals: SignalStats
    outcomes: dict[str, int]
    signalled_final_count: int
    portfolio: RiskState | None

    @property
    def age_seconds(self) -> float:
        return (datetime.now(tz=UTC) - self.computed_at).total_seconds()


class StatsService:
    """Recomputes the aggregates on an interval and holds the latest.

    ``snapshot`` is None until the first pass completes. Callers report that as
    "not computed yet" rather than as zeros -- the same rule the whole payload
    follows, since a zero that means "we have not looked" is indistinguishable
    from a zero that means "there is nothing".
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        paper_service: PaperTradingService | None,
        outcome_service: OutcomeService | None,
        interval_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # Injectable for the same reason LoopSupervisor's is: a test asserting
        # the throttle should not have to wait out a real delay, and patching
        # asyncio.sleep globally catches the test's own yields.
        self._sleep = sleep
        self._database = database
        self._settings = settings
        self._paper_service = paper_service
        self._outcome_service = outcome_service
        self._interval_seconds = interval_seconds
        self._snapshot: StatsSnapshot | None = None

    @property
    def snapshot(self) -> StatsSnapshot | None:
        return self._snapshot

    async def refresh(self) -> StatsSnapshot:
        """One pass. Separated from ``run`` so a test need not drive a loop."""
        started = asyncio.get_running_loop().time()
        schedule = schedule_from_settings(self._settings)

        async with self._database.session() as session:
            discovery = await TokenRepository(session).stats(
                backfill_max_attempts=self._settings.discovery_backfill_max_attempts
            )
            tracking = await TrackingRepository(session, schedule).stats()
            signals = await SignalRepository(session).stats()

        outcomes: dict[str, int] = {}
        signalled_final = 0
        if self._outcome_service is not None:
            outcomes = await self._outcome_service.stats()
            signalled_final = await self._outcome_service.signalled_final_count()

        portfolio = await self._paper_service.portfolio() if self._paper_service else None

        duration = asyncio.get_running_loop().time() - started
        snapshot = StatsSnapshot(
            computed_at=datetime.now(tz=UTC),
            duration_seconds=round(duration, 3),
            discovery=discovery,
            tracking=tracking,
            signals=signals,
            outcomes=outcomes,
            signalled_final_count=signalled_final,
            portfolio=portfolio,
        )
        self._snapshot = snapshot
        logger.info(
            "stats_refreshed",
            extra={"context": {"duration_seconds": snapshot.duration_seconds}},
        )
        return snapshot

    # At most this share of wall-clock time is spent running the aggregates.
    # A refresh measured at 45s against a 30s interval, so the loop was busy
    # ~60% of the time -- moving the queries off the request path had quietly
    # put them on a near-continuous one, which is heavier than the endpoint
    # ever was. A duty cycle self-limits as the tables grow, where any fixed
    # interval is a number that silently stops being true.
    MAX_DUTY_CYCLE = 0.25

    async def run(self) -> None:
        while True:
            snapshot = await self.refresh()
            floor = snapshot.duration_seconds * (1 - self.MAX_DUTY_CYCLE) / self.MAX_DUTY_CYCLE
            delay = max(self._interval_seconds, floor)
            if delay > self._interval_seconds:
                logger.warning(
                    "stats_refresh_throttled",
                    extra={
                        "context": {
                            "duration_seconds": snapshot.duration_seconds,
                            "configured_interval_seconds": self._interval_seconds,
                            "delay_seconds": round(delay, 1),
                        }
                    },
                )
            await self._sleep(delay)


def build_stats_service(
    database: Database,
    settings: Settings,
    paper_service: PaperTradingService | None,
    outcome_service: OutcomeService | None,
) -> StatsService:
    return StatsService(
        database,
        settings,
        paper_service=paper_service,
        outcome_service=outcome_service,
        interval_seconds=settings.stats_refresh_interval_seconds,
    )
