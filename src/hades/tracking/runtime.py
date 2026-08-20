"""Background lifecycle for tracking.

Same shape as the discovery runtime, and for the same reason: ``/status`` must
be able to tell "configured" from "actually running", because V1's dashboard
could not and reported healthy components that were doing nothing. Restarts are
delegated to ``LoopSupervisor`` — see ``hades/supervision.py``.
"""

from __future__ import annotations

import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.providers.http import ProviderHttpClient
from hades.providers.pumpfun import BASE_URL, SOURCE, PumpFunProvider
from hades.supervision import LoopSupervisor
from hades.tracking.schedule import TrackingSchedule
from hades.tracking.service import TrackingService

logger = logging.getLogger(__name__)


def schedule_from_settings(settings: Settings) -> TrackingSchedule:
    return TrackingSchedule(
        early_until_seconds=settings.early_tracking_seconds,
        early_interval_seconds=settings.early_snapshot_interval_seconds,
        medium_until_seconds=settings.medium_tracking_seconds,
        medium_interval_seconds=settings.medium_snapshot_interval_seconds,
        normal_until_seconds=settings.normal_tracking_seconds,
        normal_interval_seconds=settings.normal_snapshot_interval_seconds,
        long_interval_seconds=settings.long_term_snapshot_interval_seconds,
        retire_after_seconds=settings.tracking_retire_after_seconds,
    )


def build_tracking_service(database: Database, settings: Settings) -> TrackingService:
    """Wire a TrackingService from settings. Its own HTTP client, deliberately.

    Discovery and tracking each get a connection pool, so a burst in one cannot
    exhaust the other's. V1 shared no explicit limits at all and read the
    resulting timeouts as four separate provider outages.
    """
    http = ProviderHttpClient(
        SOURCE,
        base_url=BASE_URL,
        timeout_seconds=settings.provider_timeout_seconds,
        max_attempts=settings.provider_max_attempts,
        max_connections=settings.provider_max_connections,
    )
    return TrackingService(
        database,
        pumpfun=PumpFunProvider(http),
        schedule=schedule_from_settings(settings),
        max_concurrent=settings.tracking_max_concurrent,
        batch_size=settings.tracking_batch_size,
        pass_interval_seconds=settings.tracking_pass_interval_seconds,
        request_spacing_seconds=settings.tracking_request_spacing_seconds,
        stale_after_seconds=settings.tracking_stale_after_seconds,
        max_snapshot_failures=settings.tracking_max_snapshot_failures,
        failure_retry_seconds=settings.tracking_failure_retry_seconds,
    )


class TrackingRuntime:
    """Starts, stops and reports on the tracking task."""

    def __init__(self, service: TrackingService | None) -> None:
        self._service = service
        self._supervisor = LoopSupervisor("tracking", service.run) if service is not None else None

    @property
    def is_running(self) -> bool:
        return self._supervisor is not None and self._supervisor.is_running

    @property
    def last_error(self) -> str | None:
        return self._supervisor.last_error if self._supervisor else None

    @property
    def supervision(self) -> dict[str, object]:
        return self._supervisor.status() if self._supervisor else {}

    @property
    def counters(self) -> dict[str, int]:
        return self._service.counters.as_dict() if self._service else {}

    @property
    def estimated_requests_per_second(self) -> float | None:
        return self._service.estimated_requests_per_second() if self._service else None

    async def start(self) -> None:
        if self._supervisor is None:
            logger.info("tracking_disabled")
            return
        self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        if self._service is not None:
            await self._service.aclose()
        logger.info("tracking_stopped")
