"""Prometheus text exposition, hand-rolled.

No ``prometheus-client`` dependency. The format is a documented, stable,
line-oriented text protocol, and this exports a few dozen gauges from values
another object already holds — a client library would add a parallel registry
whose numbers could drift from ``/status``'s, to save writing the formatter
below. D8's reasoning about dependencies on this hardware applies: the runtime
stays as small as it can.

Everything here is a **gauge**, including the things called counters elsewhere in
this codebase. They are process-lifetime counters that reset on restart, and
Prometheus' ``counter`` type promises monotonicity that ``rate()`` relies on;
declaring them as counters would make ``rate()`` invent a spike at every restart.
As gauges they are honest, and ``hades_loop_restarts`` is there to explain any
discontinuity.

The endpoint reads only the cached snapshot and in-process state, so it is fast
by construction. It never triggers the aggregate queries — an endpoint that a
scraper hits every 15 seconds must not be able to load the database.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from hades.monitoring.stats import StatsSnapshot


class SupervisedRuntime(Protocol):
    """What the five runtimes have in common, for the exporter's purposes.

    They are five unrelated classes, so a heterogeneous dict of them is
    ``dict[str, object]`` and nothing can be read off it. This names the shape
    instead of adding a base class the runtimes do not otherwise need.
    """

    @property
    def is_running(self) -> bool: ...

    @property
    def supervision(self) -> dict[str, object]: ...

    @property
    def counters(self) -> dict[str, int]: ...


@dataclass(frozen=True, slots=True)
class LoopState:
    """One loop's supervision state, flattened for export.

    A typed record rather than the ``dict[str, object]`` the runtimes hand
    ``/status``: this module has to do arithmetic on the values, and every
    ``object`` here would otherwise need a cast at the point of use.
    """

    running: bool
    state: str
    restarts: int

    @classmethod
    def of(cls, runtime: SupervisedRuntime) -> LoopState:
        supervision = runtime.supervision
        restarts = supervision.get("restarts")
        return cls(
            running=runtime.is_running,
            state=str(supervision.get("state", "stopped")),
            restarts=restarts if isinstance(restarts, int) else 0,
        )


# Loops whose supervision state is exported. Kept explicit so a new loop has to
# be added here deliberately rather than appearing as an unlabelled series.
LOOPS: tuple[str, ...] = ("discovery", "tracking", "signals", "paper", "outcomes")

_STATE_VALUES = {"running": 1, "restarting": 2, "stopped": 0}

_ABSENT = LoopState(running=False, state="stopped", restarts=0)


def _get(loop_status: dict[str, LoopState], name: str) -> LoopState:
    return loop_status.get(name, _ABSENT)


def _line(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if labels:
        rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
        return f"{name}{{{rendered}}} {_number(value)}"
    return f"{name} {_number(value)}"


def _escape(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _number(value: float) -> str:
    """Render without scientific notation, which Prometheus accepts but humans
    reading a curl of this endpoint do not enjoy."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}"


def _metric(
    name: str, help_text: str, samples: list[tuple[dict[str, str] | None, float]]
) -> Iterator[str]:
    if not samples:
        return
    yield f"# HELP {name} {help_text}"
    yield f"# TYPE {name} gauge"
    for labels, value in samples:
        yield _line(name, value, labels)


def render(
    *,
    snapshot: StatsSnapshot | None,
    loop_status: dict[str, LoopState],
    counters: dict[str, dict[str, int]],
    database_connected: bool,
    database_latency_ms: float | None,
    uptime_seconds: float,
    research_ready_threshold: int,
) -> str:
    """Build the exposition body.

    ``snapshot`` may be None before the first refresh. Its metrics are then
    **omitted entirely** rather than exported as zero: an absent series is
    obviously absent in a graph, while a zero looks like a measurement and would
    put a false trough in every dashboard across a restart.
    """
    lines: list[str] = []

    lines.extend(
        _metric("hades_up", "1 when the process is serving.", [(None, 1)]),
    )
    lines.extend(
        _metric("hades_uptime_seconds", "Seconds since startup.", [(None, uptime_seconds)])
    )
    lines.extend(
        _metric(
            "hades_database_connected",
            "1 when the last health check reached PostgreSQL.",
            [(None, 1 if database_connected else 0)],
        )
    )
    if database_latency_ms is not None:
        lines.extend(
            _metric(
                "hades_database_latency_ms",
                "Round trip of the health-check query.",
                [(None, database_latency_ms)],
            )
        )

    lines.extend(
        _metric(
            "hades_loop_running",
            "1 when the loop body is executing. 0 covers both stopped and waiting in backoff.",
            [({"loop": name}, 1 if _get(loop_status, name).running else 0) for name in LOOPS],
        )
    )
    lines.extend(
        _metric(
            "hades_loop_state",
            "0 stopped, 1 running, 2 restarting.",
            [
                ({"loop": name}, _STATE_VALUES.get(_get(loop_status, name).state, 0))
                for name in LOOPS
            ],
        )
    )
    lines.extend(
        _metric(
            "hades_loop_restarts",
            "Times the loop has been restarted after dying. Non-zero is a finding.",
            [({"loop": name}, float(_get(loop_status, name).restarts)) for name in LOOPS],
        )
    )

    # The in-process counters, one series per loop and counter name. Gauges by
    # deliberate choice -- see the module docstring.
    samples: list[tuple[dict[str, str] | None, float]] = []
    for loop, values in sorted(counters.items()):
        for key, value in sorted(values.items()):
            samples.append(({"loop": loop, "counter": key}, float(value)))
    lines.extend(
        _metric(
            "hades_loop_counter",
            "Per-loop process-lifetime counters. Reset on restart, hence a gauge.",
            samples,
        )
    )

    if snapshot is None:
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        _metric(
            "hades_stats_age_seconds",
            "Age of the cached database aggregates below. Non-trivial by design.",
            [(None, snapshot.age_seconds)],
        )
    )
    lines.extend(
        _metric(
            "hades_stats_refresh_duration_seconds",
            "How long the aggregate queries took. This is why they are cached.",
            [(None, snapshot.duration_seconds)],
        )
    )

    discovery = snapshot.discovery
    lines.extend(_metric("hades_tokens_total", "Rows in `tokens`.", [(None, discovery.total)]))
    lines.extend(
        _metric(
            "hades_tokens_by_state",
            "Tokens per lifecycle state.",
            [
                ({"state": state}, float(count))
                for state, count in sorted(discovery.by_state.items())
            ],
        )
    )
    lines.extend(
        _metric(
            "hades_tokens_backfill_exhausted",
            "Tokens that will never get a created_at. Growing is lost universe coverage.",
            [(None, discovery.backfill_exhausted)],
        )
    )
    if discovery.median_discovery_latency_ms is not None:
        lines.extend(
            _metric(
                "hades_discovery_latency_ms",
                "Median of (discovered_at - created_at).",
                [(None, discovery.median_discovery_latency_ms)],
            )
        )

    tracking = snapshot.tracking
    lines.extend(
        _metric("hades_tracking_now", "Tokens being tracked.", [(None, tracking.tracking_now)])
    )
    lines.extend(
        _metric(
            "hades_tracking_eligible_waiting",
            "Trackable tokens declined for lack of capacity. No later analysis recovers these.",
            [(None, tracking.eligible_waiting)],
        )
    )
    lines.extend(
        _metric(
            "hades_snapshots_total",
            "Rows in `market_snapshots`.",
            [(None, tracking.snapshots_total)],
        )
    )
    lines.extend(
        _metric(
            "hades_snapshots_last_hour",
            "Snapshots in the last hour. Zero means nothing is being collected.",
            [(None, tracking.snapshots_last_hour)],
        )
    )
    if tracking.oldest_due_seconds is not None:
        lines.extend(
            _metric(
                "hades_tracking_oldest_due_seconds",
                "How far past its scheduled time the most overdue token is.",
                [(None, tracking.oldest_due_seconds)],
            )
        )

    signals = snapshot.signals
    lines.extend(
        _metric(
            "hades_observations_total",
            "Feature vectors stored.",
            [(None, signals.observations_total)],
        )
    )
    lines.extend(
        _metric("hades_signals_total", "Research signals.", [(None, signals.signals_total)])
    )
    lines.extend(
        _metric(
            "hades_signals_last_hour",
            "Signals in the last hour.",
            [(None, signals.signals_last_hour)],
        )
    )
    if signals.signal_rate is not None:
        lines.extend(
            _metric(
                "hades_signal_rate",
                "Signals per observation. The denominator is the point.",
                [(None, signals.signal_rate)],
            )
        )

    lines.extend(
        _metric(
            "hades_signalled_final_count",
            "Observations that both fired a signal and have a final outcome.",
            [(None, snapshot.signalled_final_count)],
        )
    )
    lines.extend(
        _metric(
            "hades_research_ready_threshold",
            "Configured threshold for signalled_final_count.",
            [(None, research_ready_threshold)],
        )
    )
    for key, value in sorted(snapshot.outcomes.items()):
        lines.extend(_metric(f"hades_{key}", f"Outcome engine: {key}.", [(None, float(value))]))

    portfolio = snapshot.portfolio
    if portfolio is not None:
        lines.extend(
            _metric(
                "hades_paper_balance_sol",
                "Simulated balance. Paper only -- never real money.",
                [(None, portfolio.balance_sol)],
            )
        )
        lines.extend(
            _metric(
                "hades_paper_equity_sol",
                "Simulated equity. Paper only.",
                [(None, portfolio.current_equity_sol)],
            )
        )
        lines.extend(
            _metric(
                "hades_paper_open_positions",
                "Open simulated positions.",
                [(None, portfolio.open_positions)],
            )
        )
        drawdown = portfolio.drawdown_fraction
        if drawdown is not None:
            lines.extend(
                _metric(
                    "hades_paper_drawdown_fraction",
                    "Fall from the realised-PnL high-water mark.",
                    [(None, drawdown)],
                )
            )

    lines.append("")
    return "\n".join(lines)
