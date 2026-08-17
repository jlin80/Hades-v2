"""The tracking loop.

One pass does three things: retire what has aged out, admit new tokens into the
freed slots, and snapshot whatever is due. In that order, because retiring
first is what makes the slots available to admit into.

## Why there is a capacity limit at all

Measured: the primary sustains 1.64 req/s; Pump.fun creates 0.24 to 0.55 tokens/s.
The spec's default schedule costs 434 snapshots per token per day, so tracking
the whole universe would need 104 to 239 req/s — **64x to 146x over capacity**.

This is not a tuning problem. The system tracks a *sample*, and the honest
consequences are:

* ``eligible_waiting`` in ``/status`` is the size of the sample we are declining
  to take. It is reported because no later analysis recovers those tokens.
* Admission is newest-first, so at capacity the selection is biased toward
  tokens created when a slot happened to be free. That is close to random in
  time, which is the best available, but it is **not** a uniform sample of the
  universe and research must not treat it as one.

Admission is one-way: once a token is in, it keeps its slot until it ages out.
Churning slots to sample more tokens would produce many truncated series, and a
truncated series cannot answer a question about what happens over the first
minutes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hades.db.engine import Database
from hades.providers.errors import ProviderError, ProviderRateLimitedError
from hades.providers.pumpfun import PumpFunProvider
from hades.tracking.repository import TrackingRepository
from hades.tracking.schedule import TrackingSchedule

logger = logging.getLogger(__name__)


@dataclass
class TrackingCounters:
    """In-process counters. The database remains the source of truth."""

    passes: int = 0
    admitted: int = 0
    snapshots_taken: int = 0
    snapshots_failed: int = 0
    stale_observed: int = 0
    migrated: int = 0
    retired: int = 0
    abandoned: int = 0
    rate_limited: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def as_dict(self) -> dict[str, int]:
        return {
            "passes": self.passes,
            "admitted": self.admitted,
            "snapshots_taken": self.snapshots_taken,
            "snapshots_failed": self.snapshots_failed,
            "stale_observed": self.stale_observed,
            "migrated": self.migrated,
            "retired": self.retired,
            "abandoned": self.abandoned,
            "rate_limited": self.rate_limited,
        }


class TrackingService:
    """Takes market snapshots on an adaptive schedule, within a fixed budget."""

    def __init__(
        self,
        database: Database,
        *,
        pumpfun: PumpFunProvider | None = None,
        schedule: TrackingSchedule | None = None,
        max_concurrent: int = 40,
        batch_size: int = 20,
        pass_interval_seconds: float = 2.0,
        request_spacing_seconds: float = 0.2,
        stale_after_seconds: float = 60.0,
        max_snapshot_failures: int = 3,
        failure_retry_seconds: float = 30.0,
    ) -> None:
        self._database = database
        self._pumpfun = pumpfun or PumpFunProvider()
        self._schedule = schedule or TrackingSchedule()
        self._max_concurrent = max_concurrent
        self._batch_size = batch_size
        self._pass_interval_seconds = pass_interval_seconds
        self._request_spacing_seconds = request_spacing_seconds
        self._stale_after_seconds = stale_after_seconds
        self._max_snapshot_failures = max_snapshot_failures
        self._failure_retry_seconds = failure_retry_seconds
        self.counters = TrackingCounters()

    @property
    def schedule(self) -> TrackingSchedule:
        return self._schedule

    def estimated_requests_per_second(self) -> float:
        """What holding a full complement of tracked tokens is expected to cost."""
        return self._schedule.estimated_requests_per_second(self._max_concurrent)

    async def retire_and_admit(self) -> int:
        """Free aged-out slots, then fill them. Returns tokens admitted."""
        async with self._database.session() as session:
            repository = TrackingRepository(session, self._schedule)
            retired = await repository.retire_overdue()
            self.counters.retired += retired

            in_tracking = await repository.count_tracking()
            slots = max(0, self._max_concurrent - in_tracking)
            admitted = await repository.admit(slots=slots)

        self.counters.admitted += len(admitted)
        if retired or admitted:
            logger.info(
                "tracking_admission",
                extra={
                    "context": {
                        "retired": retired,
                        "admitted": len(admitted),
                        "tracking_now": in_tracking + len(admitted),
                        "capacity": self._max_concurrent,
                    }
                },
            )
        return len(admitted)

    async def snapshot_due(self) -> int:
        """Snapshot every due token, up to the batch size. Returns successes."""
        async with self._database.session() as session:
            due = await TrackingRepository(session, self._schedule).due(limit=self._batch_size)
        if not due:
            return 0

        taken = 0
        for index, token in enumerate(due):
            if index:
                # Deliberate spacing. The primary's rate limit is unpublished
                # and it is our largest technical risk; a batch fired all at
                # once is exactly the shape most likely to find the ceiling.
                await asyncio.sleep(self._request_spacing_seconds)

            try:
                snapshot = await self._pumpfun.fetch_snapshot(token.token_address)
            except ProviderRateLimitedError as exc:
                # Stop the pass rather than push harder into a closing door.
                self.counters.rate_limited += 1
                logger.warning(
                    "tracking_rate_limited",
                    extra={"context": {"retry_after_seconds": exc.retry_after_seconds}},
                )
                await asyncio.sleep(exc.retry_after_seconds or self._pass_interval_seconds)
                break
            except ProviderError as exc:
                self.counters.snapshots_failed += 1
                async with self._database.session() as session:
                    abandoned = await TrackingRepository(session, self._schedule).record_failure(
                        token,
                        max_failures=self._max_snapshot_failures,
                        retry_after=self._failure_retry_seconds,
                    )
                if abandoned:
                    self.counters.abandoned += 1
                logger.info(
                    "snapshot_failed",
                    extra={
                        "context": {
                            "token_address": token.token_address,
                            "failures": token.snapshot_failures + 1,
                            "abandoned": abandoned,
                            "reason": str(exc),
                        }
                    },
                )
                continue

            async with self._database.session() as session:
                await TrackingRepository(session, self._schedule).record_snapshot(
                    token, snapshot, stale_after_seconds=self._stale_after_seconds
                )

            taken += 1
            self.counters.snapshots_taken += 1
            if snapshot.is_complete:
                self.counters.migrated += 1
            data_age = snapshot.provider_data_age_seconds
            if data_age is not None and data_age > self._stale_after_seconds:
                self.counters.stale_observed += 1

        return taken

    async def run_once(self) -> int:
        """One full pass. Returns how many snapshots were taken."""
        self.counters.passes += 1
        await self.retire_and_admit()
        return await self.snapshot_due()

    async def run(self) -> None:
        """Loop until cancelled."""
        logger.info(
            "tracking_started",
            extra={
                "context": {
                    "max_concurrent": self._max_concurrent,
                    "retire_after_seconds": self._schedule.retire_after_seconds,
                    "snapshots_per_token": round(self._schedule.snapshots_per_token(), 1),
                    "estimated_requests_per_second": round(self.estimated_requests_per_second(), 2),
                }
            },
        )
        while True:
            await self.run_once()
            await asyncio.sleep(self._pass_interval_seconds)

    async def aclose(self) -> None:
        await self._pumpfun.aclose()
