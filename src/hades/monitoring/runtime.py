"""Background lifecycle for the stats refresher.

Same shape as every other runtime, and supervised for the same reason: if this
loop dies without restarting, ``/status`` and ``/metrics`` keep serving the last
snapshot forever. That is the worst of the failure modes this codebase keeps
guarding against — an endpoint reporting confidently on a system it stopped
observing — and the only thing that makes it survivable is that the snapshot
carries its own age, so a stuck refresher shows up as a rising
``hades_stats_age_seconds`` rather than as numbers that never change.
"""

from __future__ import annotations

import logging

from hades.monitoring.stats import StatsService
from hades.supervision import LoopSupervisor

logger = logging.getLogger(__name__)


class StatsRuntime:
    """Starts, stops and reports on the stats refresh task."""

    def __init__(self, service: StatsService | None) -> None:
        self._service = service
        self._supervisor = LoopSupervisor("stats", service.run) if service is not None else None

    @property
    def is_running(self) -> bool:
        return self._supervisor is not None and self._supervisor.is_running

    @property
    def last_error(self) -> str | None:
        return self._supervisor.last_error if self._supervisor else None

    @property
    def service(self) -> StatsService | None:
        return self._service

    async def start(self) -> None:
        if self._supervisor is None:
            logger.info("stats_disabled")
            return
        self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        logger.info("stats_stopped")
