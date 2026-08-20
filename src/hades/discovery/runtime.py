"""Background lifecycle for discovery.

Owns the task, and — more importantly — owns the truth about whether it is
actually running. ``/status`` asks this object, not the settings file.

Hades V1's dashboard showed healthy components that were doing nothing, because
"configured" and "running" were never distinguished anywhere. A crashed
collector that still reports `enabled: true` is worse than one that reports
nothing, so ``is_running`` is derived from the loop and ``last_error`` retains
why it stopped.

Restarting is delegated to ``LoopSupervisor``. Recording the crash was never
enough on its own: discovery died here and stayed dead for over seven hours,
and because it is the head of the pipeline everything downstream idled with it.
See ``hades/supervision.py``.
"""

from __future__ import annotations

import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.discovery.service import DiscoveryService
from hades.providers.http import ProviderHttpClient
from hades.providers.pumpfun import BASE_URL, SOURCE, PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider
from hades.supervision import LoopSupervisor

logger = logging.getLogger(__name__)


def build_service(database: Database, settings: Settings) -> DiscoveryService:
    """Wire a DiscoveryService from settings. The only place providers are built."""
    http = ProviderHttpClient(
        SOURCE,
        base_url=BASE_URL,
        timeout_seconds=settings.provider_timeout_seconds,
        max_attempts=settings.provider_max_attempts,
        max_connections=settings.provider_max_connections,
    )
    return DiscoveryService(
        database,
        pumpfun=PumpFunProvider(http),
        pumpportal=PumpPortalProvider(),
        poll_interval_seconds=settings.discovery_poll_interval_seconds,
        poll_limit=settings.discovery_poll_limit,
        backfill_limit=settings.discovery_backfill_limit,
        backfill_max_attempts=settings.discovery_backfill_max_attempts,
    )


class DiscoveryRuntime:
    """Starts, stops and reports on the discovery task."""

    def __init__(self, service: DiscoveryService | None) -> None:
        self._service = service
        # D5 applied to a background task: the process must survive to report
        # the failure, so the supervisor never lets an exception escape into
        # the lifespan. What it adds is that the loop then comes back.
        self._supervisor = LoopSupervisor("discovery", service.run) if service is not None else None

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

    async def start(self) -> None:
        if self._supervisor is None:
            logger.info("discovery_disabled")
            return
        self._supervisor.start()
        logger.info("discovery_started")

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        if self._service is not None:
            await self._service.aclose()
        logger.info("discovery_stopped")
