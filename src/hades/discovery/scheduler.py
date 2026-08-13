"""The background discovery loop.

One asyncio task, one interval, one job. This is the whole scheduler: task.md
forbids a message broker or an event bus until something measurably needs one,
and v1's twelve-runtime event loop is the reason.

The loop's contract is that it does not die. A provider outage, a database blip
or an unanticipated bug must produce a logged error and a next iteration, not a
silently stopped task — a scheduler that exits leaves the system looking healthy
while collecting nothing, which is exactly how v1 ran for weeks.
"""

import asyncio

from hades.discovery.service import DiscoveryService
from hades.observability.logging import get_logger

logger = get_logger(__name__)


class DiscoveryScheduler:
    """Runs :meth:`DiscoveryService.run_once` on a fixed interval."""

    def __init__(self, service: DiscoveryService, interval_seconds: float) -> None:
        self._service = service
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the loop. Idempotent."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="discovery-scheduler")
        logger.info("discovery_scheduler_started", interval_seconds=self._interval)

    async def stop(self) -> None:
        """Cancel the loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("discovery_scheduler_stopped")

    async def _run(self) -> None:
        while True:
            try:
                await self._service.run_once()
            except asyncio.CancelledError:
                # Shutdown, not a fault. Propagate so stop() completes.
                raise
            except Exception:
                # A broad catch is correct here and nowhere else. This is the
                # supervisor boundary: anything that escapes run_once() is a bug
                # we have not anticipated, and letting it kill the task would
                # stop all data collection while /health still reported fine.
                # The exception is logged in full, never discarded.
                logger.exception("discovery_iteration_crashed")

            await asyncio.sleep(self._interval)
