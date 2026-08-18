"""Background lifecycle for outcome labelling.

Same shape as the discovery, tracking, signal and paper runtimes: ``/status``
must be able to tell "configured" from "actually running", and a crashed loop
must not take the API down with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.outcomes.labels import DEFAULT_BARRIERS
from hades.outcomes.service import OutcomeService

logger = logging.getLogger(__name__)


def build_outcome_service(database: Database, settings: Settings) -> OutcomeService:
    return OutcomeService(
        database,
        barriers=DEFAULT_BARRIERS,
        batch_size=settings.outcomes_batch_size,
        pass_interval_seconds=settings.outcomes_pass_interval_seconds,
    )


class OutcomeRuntime:
    """Starts, stops and reports on the outcome-labelling task."""

    def __init__(self, service: OutcomeService | None) -> None:
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

    @property
    def service(self) -> OutcomeService | None:
        """Exposed so ``/status`` can read live labelling stats on demand."""
        return self._service

    async def start(self) -> None:
        service = self._service
        if service is None:
            logger.info("outcomes_disabled")
            return
        self._task = asyncio.create_task(self._supervise(service), name="outcome-labelling")

    async def _supervise(self, service: OutcomeService) -> None:
        try:
            await service.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("outcomes_crashed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("outcomes_stopped")
