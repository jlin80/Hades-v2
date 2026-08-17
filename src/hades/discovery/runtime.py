"""Background lifecycle for discovery.

Owns the task, and — more importantly — owns the truth about whether it is
actually running. ``/status`` asks this object, not the settings file.

Hades V1's dashboard showed healthy components that were doing nothing, because
"configured" and "running" were never distinguished anywhere. A crashed
collector that still reports `enabled: true` is worse than one that reports
nothing, so ``is_running`` is derived from the task and ``last_error`` retains
why it stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.discovery.service import DiscoveryService
from hades.providers.http import ProviderHttpClient
from hades.providers.pumpfun import BASE_URL, SOURCE, PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider

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
    )


class DiscoveryRuntime:
    """Starts, stops and reports on the discovery task."""

    def __init__(self, service: DiscoveryService | None) -> None:
        self._service = service
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def counters(self) -> dict[str, int]:
        return self._service.counters.as_dict() if self._service else {}

    async def start(self) -> None:
        service = self._service
        if service is None:
            logger.info("discovery_disabled")
            return
        self._task = asyncio.create_task(self._supervise(service), name="discovery")
        logger.info("discovery_started")

    async def _supervise(self, service: DiscoveryService) -> None:
        """Run discovery; if it dies, record why and let the API stay up.

        D5 applied to a background task: the process must survive to report the
        failure. A collector that takes the whole container down with it also
        takes down the endpoint that would have explained the outage.
        """
        try:
            await service.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("discovery_crashed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._service is not None:
            await self._service.aclose()
        logger.info("discovery_stopped")
