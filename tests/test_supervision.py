"""The restart behaviour, and the reason it exists.

Discovery crashed on CT202 and stayed down for 7h20m because the old runtimes
recorded the exception and returned. Every test here is about that outage: the
loop must come back, the backoff must not hammer a provider that is already
failing, the recovery must stay visible after the fact, and the error string
must name the actual cause rather than the number of sub-exceptions.
"""

from __future__ import annotations

import asyncio

from hades.supervision import LoopSupervisor, describe_exception


async def _settle() -> None:
    """Yield enough times for the supervisor's own awaits to make progress."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_crashed_loop_is_restarted() -> None:
    """The whole point. The old runtime would leave attempts at 1 forever."""
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("provider blew up")
        await asyncio.sleep(3600)

    supervisor = LoopSupervisor("flaky", flaky, base_delay_seconds=0.0, max_delay_seconds=0.0)
    supervisor.start()
    await _settle()

    assert attempts == 3
    assert supervisor.is_running
    assert supervisor.restarts == 2
    await supervisor.stop()


async def test_backoff_grows_and_is_capped() -> None:
    """A loop failing instantly must not retry instantly.

    The crash that killed discovery was most likely transient provider or
    database trouble; restarting in a tight loop against a service that is
    already rate-limiting is how a short outage becomes a ban.
    """
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    async def always_fails() -> None:
        raise RuntimeError("nope")

    supervisor = LoopSupervisor(
        "doomed",
        always_fails,
        base_delay_seconds=1.0,
        max_delay_seconds=4.0,
        sleep=record,
    )
    supervisor.start()
    await _settle()
    await supervisor.stop()

    assert delays[:5] == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_backoff_resets_only_after_the_loop_proves_it_is_healthy() -> None:
    """Surviving briefly is not recovery.

    Without ``healthy_after_seconds`` a loop that dies 200 ms into every attempt
    resets its own delay each time and never backs off at all.
    """

    async def dies_immediately() -> None:
        raise RuntimeError("nope")

    supervisor = LoopSupervisor(
        "impatient",
        dies_immediately,
        base_delay_seconds=0.0,
        max_delay_seconds=0.0,
        healthy_after_seconds=3600.0,
    )
    supervisor.start()
    await _settle()
    await supervisor.stop()

    # It kept restarting rather than treating each short life as a recovery.
    assert supervisor.restarts > 1


async def test_a_loop_waiting_in_backoff_does_not_report_itself_as_running() -> None:
    """The V1 dashboard bug, one level up.

    A supervised task is always alive, so ``is_running`` derived from the task
    would be True while the loop sat in backoff doing nothing.
    """

    async def always_fails() -> None:
        raise RuntimeError("nope")

    supervisor = LoopSupervisor(
        "doomed", always_fails, base_delay_seconds=3600.0, max_delay_seconds=3600.0
    )
    supervisor.start()
    await _settle()

    assert supervisor.state == "restarting"
    assert supervisor.is_running is False
    await supervisor.stop()


async def test_last_error_survives_a_successful_restart() -> None:
    """A loop that crashes hourly and recovers looks healthy at every instant.

    Clearing the error on recovery is what would make that pattern invisible.
    """
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        await asyncio.sleep(3600)

    supervisor = LoopSupervisor("flaky", flaky, base_delay_seconds=0.0, max_delay_seconds=0.0)
    supervisor.start()
    await _settle()

    assert supervisor.is_running
    assert supervisor.last_error == "RuntimeError: transient"
    assert supervisor.status()["restarts"] == 1
    await supervisor.stop()


async def test_a_loop_that_returns_quietly_is_treated_as_a_failure() -> None:
    """Returning is not a clean exit for something meant to run until cancelled.

    It is the quieter failure of the two, because it leaves ``last_error`` empty.
    """
    calls = 0

    async def returns_immediately() -> None:
        nonlocal calls
        calls += 1

    supervisor = LoopSupervisor(
        "quitter", returns_immediately, base_delay_seconds=0.0, max_delay_seconds=0.0
    )
    supervisor.start()
    await _settle()
    await supervisor.stop()

    assert calls > 1
    assert supervisor.last_error == "loop returned without being cancelled"


async def test_stop_does_not_restart_the_loop() -> None:
    """Cancellation is the one exit that must not be retried."""
    calls = 0

    async def loop() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(3600)

    supervisor = LoopSupervisor("loop", loop)
    supervisor.start()
    await _settle()
    await supervisor.stop()
    await _settle()

    assert calls == 1
    assert supervisor.state == "stopped"


def test_describe_exception_names_the_cause_inside_an_exception_group() -> None:
    """The string that recorded a seven-hour outage said nothing about it.

    ``f"{type(exc).__name__}: {exc}"`` on discovery's TaskGroup failure produced
    "ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)" — a
    count, with no mention of what actually failed.
    """
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionResetError("peer gone")])

    described = describe_exception(group)

    assert "ConnectionResetError" in described
    assert "peer gone" in described


def test_describe_exception_flattens_nested_groups() -> None:
    """Discovery nests a TaskGroup inside the supervised call, so groups nest."""
    inner = ExceptionGroup("inner", [ValueError("bad row")])
    outer = ExceptionGroup("outer", [inner])

    described = describe_exception(outer)

    assert "ValueError: bad row" in described
