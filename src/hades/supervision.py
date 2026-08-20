"""Restart supervision for the background loops.

Every phase from 2 onwards ends in a task that is supposed to run forever, and
each one had the same shape: catch the exception, store it on ``last_error``,
log it, return. The API survived — which was the point, D5 — but the loop was
then dead for the rest of the process lifetime, and nothing ever started it
again.

That is not hypothetical. Measured on CT202 on 2026-08-20: discovery had been
down for **7h20m** behind ``last_error: "ExceptionGroup: unhandled errors in a
TaskGroup (1 sub-exception)"``. Tracking, signals, paper and outcomes all still
reported ``running: true`` and all had frozen counters, because with discovery
dead there were no new tokens to admit and the pipeline starved from the head.
One crashed loop stopped the entire research run, silently, for a third of a day.

So the loops restart. Three things that follow from *how* they failed:

* **Backoff is capped and exponential.** The failure that killed discovery was
  almost certainly a transient database or provider error. Restarting instantly
  in a tight loop against a provider that is rate-limiting us is how a small
  outage becomes a ban.
* **The backoff resets only after the loop proves it is healthy.** A loop that
  crashes 200ms into every attempt must not have its delay reset by the attempt
  itself; ``healthy_after_seconds`` is what separates "it recovered" from "it is
  crash-looping quickly".
* **"Running" stops being a boolean.** A supervised task is always alive, so
  ``is_running`` alone would report ``true`` while the loop sat in backoff — the
  exact lie about a healthy-looking dead component that ``DiscoveryRuntime``'s
  docstring was written to prevent. ``state`` distinguishes the three cases and
  ``/status`` reports it alongside ``restarts``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

SupervisorState = Literal["stopped", "running", "restarting"]


def describe_exception(exc: BaseException) -> str:
    """Render an exception in a form that names the actual cause.

    ``ExceptionGroup`` is why this exists. ``f"{type(exc).__name__}: {exc}"`` on
    one produces ``"ExceptionGroup: unhandled errors in a TaskGroup (1
    sub-exception)"`` — the number of failures and not one word about what they
    were. Discovery runs its two loops in a ``TaskGroup``, so that string was
    the *entire* record of a seven-hour outage.

    Groups are flattened recursively, since a nested TaskGroup nests its group.
    """
    if isinstance(exc, BaseExceptionGroup):
        inner = ", ".join(describe_exception(sub) for sub in exc.exceptions)
        return f"{type(exc).__name__}({inner})"
    return f"{type(exc).__name__}: {exc}"


class LoopSupervisor:
    """Runs one never-ending coroutine, restarting it when it dies.

    ``factory`` is called for each attempt rather than being handed a single
    coroutine object, because a coroutine cannot be awaited twice.
    """

    def __init__(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 60.0,
        healthy_after_seconds: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._name = name
        self._factory = factory
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._healthy_after_seconds = healthy_after_seconds
        # Injectable so a test can assert the backoff schedule without waiting
        # out a real one. Patching asyncio.sleep globally instead would also
        # capture the test's own yields, which is how the first version of
        # test_backoff_grows_and_is_capped measured itself.
        self._sleep = sleep

        self._task: asyncio.Task[None] | None = None
        self._state: SupervisorState = "stopped"
        self._last_error: str | None = None
        self._restarts = 0
        self._last_restart_at: datetime | None = None

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def is_running(self) -> bool:
        """True only while the supervised loop body is actually executing.

        Deliberately False during backoff. A component waiting to be restarted
        is not doing its job, and reporting otherwise is the V1 dashboard bug.
        """
        return self._state == "running"

    @property
    def last_error(self) -> str | None:
        """Why the loop last died. Retained across a successful restart.

        Kept rather than cleared on recovery: a loop that crashes every ten
        minutes and recovers each time looks perfectly healthy at any instant,
        and ``restarts`` plus this string is what makes that pattern visible.
        """
        return self._last_error

    @property
    def restarts(self) -> int:
        return self._restarts

    @property
    def last_restart_at(self) -> datetime | None:
        return self._last_restart_at

    def status(self) -> dict[str, object]:
        """The supervision fields ``/status`` merges into each component."""
        return {
            "state": self._state,
            "restarts": self._restarts,
            "last_restart_at": (
                self._last_restart_at.isoformat() if self._last_restart_at else None
            ),
        }

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._supervise(), name=self._name)

    async def _supervise(self) -> None:
        delay = self._base_delay_seconds
        while True:
            started = time.monotonic()
            self._state = "running"
            try:
                await self._factory()
            except asyncio.CancelledError:
                self._state = "stopped"
                raise
            except Exception as exc:
                self._last_error = describe_exception(exc)
                logger.exception(
                    "loop_crashed",
                    extra={
                        "context": {
                            "loop": self._name,
                            "reason": self._last_error,
                            "uptime_seconds": round(time.monotonic() - started, 3),
                            "restarts": self._restarts,
                        }
                    },
                )
            else:
                # Returning without raising is also a failure for a loop that is
                # supposed to run until cancelled — and a quieter one, since it
                # leaves last_error empty. Say so rather than treating a silent
                # return as a clean exit.
                self._last_error = "loop returned without being cancelled"
                logger.error(
                    "loop_returned",
                    extra={"context": {"loop": self._name, "restarts": self._restarts}},
                )

            if time.monotonic() - started >= self._healthy_after_seconds:
                delay = self._base_delay_seconds

            self._state = "restarting"
            self._restarts += 1
            self._last_restart_at = datetime.now(tz=UTC)
            logger.warning(
                "loop_restarting",
                extra={
                    "context": {
                        "loop": self._name,
                        "delay_seconds": delay,
                        "restarts": self._restarts,
                    }
                },
            )
            await self._sleep(delay)
            delay = min(delay * 2, self._max_delay_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._state = "stopped"
