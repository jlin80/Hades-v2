"""``OutcomeRuntime``: same shape as the other background runtimes.

Only checks the wiring -- ``build_outcome_service`` constructs a service from
settings, and the runtime reports "configured" vs "actually running" the same
way discovery/tracking/signals/paper do. The labelling logic itself is covered
by ``tests/test_outcome_service.py``.
"""

from __future__ import annotations

import asyncio

from hades.config import Settings
from hades.outcomes.runtime import OutcomeRuntime, build_outcome_service


def test_build_outcome_service_uses_the_configured_batch_and_interval() -> None:
    settings = Settings(outcomes_batch_size=50, outcomes_pass_interval_seconds=5.0)

    class DummyDatabase:
        pass

    service = build_outcome_service(DummyDatabase(), settings)  # type: ignore[arg-type]

    assert service._batch_size == 50
    assert service._pass_interval_seconds == 5.0


def test_no_service_means_not_running_and_no_task() -> None:
    runtime = OutcomeRuntime(None)
    assert runtime.is_running is False
    assert runtime.service is None
    assert runtime.counters == {}


async def test_start_with_no_service_does_not_raise() -> None:
    runtime = OutcomeRuntime(None)
    await runtime.start()
    assert runtime.is_running is False
    await runtime.stop()  # must not raise even though nothing was started


async def test_a_crashing_service_is_reported_not_propagated() -> None:
    class ExplodingService:
        counters = type("C", (), {"as_dict": lambda self: {}})()

        async def run(self) -> None:
            raise RuntimeError("boom")

    runtime = OutcomeRuntime(ExplodingService())  # type: ignore[arg-type]
    await runtime.start()
    await asyncio.sleep(0.05)

    assert runtime.is_running is False
    assert runtime.last_error is not None
    assert "boom" in runtime.last_error
    await runtime.stop()
