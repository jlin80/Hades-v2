"""Background lifecycle for outcome labelling.

Same shape as the discovery, tracking, signal and paper runtimes: ``/status``
must be able to tell "configured" from "actually running", a crashed loop must
not take the API down with it, and ``LoopSupervisor`` brings it back afterwards.
"""

from __future__ import annotations

import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.outcomes.labels import DEFAULT_BARRIERS
from hades.outcomes.service import OutcomeService
from hades.supervision import LoopSupervisor

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
        self._supervisor = (
            LoopSupervisor("outcome-labelling", service.run) if service is not None else None
        )

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
    def service(self) -> OutcomeService | None:
        """Exposed so ``/status`` can read live labelling stats on demand."""
        return self._service

    async def start(self) -> None:
        if self._supervisor is None:
            logger.info("outcomes_disabled")
            return
        self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        logger.info("outcomes_stopped")
