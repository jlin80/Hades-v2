"""Response models for the observability endpoints.

Every field here is something the process actually measured. There is no
placeholder counter: a metric whose producing phase does not exist yet is
absent from the schema, not present as a zero. A zero that means "not built"
is indistinguishable from a zero that means "nothing happened", and the second
is a finding while the first is noise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DatabaseStatus(BaseModel):
    connected: bool
    latency_ms: float | None = None
    error: str | None = None


class SupervisionStatus(BaseModel):
    """How the component's background loop has behaved over the process's life.

    ``running`` is an instant. It cannot distinguish a loop that has been up
    since boot from one that has crashed forty times and happens to be up right
    now, and the second is the interesting case: discovery spent 7h20m dead on
    CT202 behind a ``running: false`` nobody was watching, and the restart that
    now recovers it would also have hidden the crash entirely without a count.
    """

    state: Literal["stopped", "running", "restarting"] = "stopped"
    restarts: int = Field(
        default=0,
        description="Times the loop has been restarted after dying. Non-zero is a finding.",
    )
    last_restart_at: datetime | None = None


class ComponentStatus(BaseModel):
    """The four fields every background component reports, plus supervision.

    Discovery, tracking, signals, paper and outcomes all had these declared
    independently, which is how ``supervision`` would have been added to four
    of the five.
    """

    enabled: bool
    running: bool
    last_error: str | None = Field(
        default=None,
        description=(
            "Why the loop last died, retained across a successful restart. "
            "Read it with `supervision.restarts`, not on its own."
        ),
    )
    counters: dict[str, int] = Field(default_factory=dict)
    supervision: SupervisionStatus = Field(default_factory=SupervisionStatus)


class HealthResponse(BaseModel):
    """Liveness plus the one dependency we cannot work without."""

    status: Literal["healthy", "degraded"]
    version: str
    environment: str
    database: DatabaseStatus
    trading_mode: Literal["paper"] = "paper"
    is_live: Literal[False] = False


class DiscoveryStatus(ComponentStatus):
    """Discovery state, all of it measured.

    ``running`` is whether the background loop is actually executing, not
    whether it was configured to start and not whether a supervisor is holding
    a task open for it. Hades V1's dashboard reported healthy components that
    were doing nothing; the distinction is the whole point.
    """

    tokens_total: int | None = None
    tokens_by_state: dict[str, int] | None = None
    tokens_with_created_at: int | None = None
    tokens_backfill_exhausted: int | None = Field(
        default=None,
        description=(
            "Tokens that will never get a created_at from the primary, having spent "
            "their retry budget. Without token_age they cannot be tracked, so this "
            "growing is a real loss of universe coverage, not a cosmetic counter."
        ),
    )
    last_discovered_at: datetime | None = None
    median_discovery_latency_ms: float | None = Field(
        default=None,
        description=(
            "Median of (discovered_at - created_at). How far behind creation our "
            "first sighting is — the reason those are two columns and not one."
        ),
    )


class TrackingStatus(ComponentStatus):
    """Tracking state, all of it measured."""

    max_concurrent: int
    retire_after_seconds: float
    snapshots_per_token: float
    estimated_requests_per_second: float | None = Field(
        default=None,
        description=(
            "Average rate the current capacity implies. Surfaced because the "
            "primary's real limit is unpublished and 1.64 req/s is all that has "
            "been measured as safe."
        ),
    )

    tracking_now: int | None = None
    eligible_waiting: int | None = Field(
        default=None,
        description=(
            "Tokens that could be tracked but are not, because capacity is full. "
            "This is the sample we are declining to take, and no later analysis "
            "recovers it."
        ),
    )
    snapshots_total: int | None = None
    snapshots_last_hour: int | None = None
    stale_snapshots: int | None = None
    tokens_retired: int | None = None
    tokens_migrated: int | None = None
    tokens_dead: int | None = None
    oldest_due_seconds: float | None = Field(
        default=None,
        description=(
            "How far past its scheduled time the most overdue token is. The one "
            "number that says whether the tracker is keeping up."
        ),
    )


class SignalStatus(ComponentStatus):
    """Signal research state.

    ``signals_total`` alone is not a result. It is reported next to
    ``observations_total`` because §17 asks how many signals there were, and
    that is meaningless without how many chances there were to fire.
    """

    strategy: str | None = None
    strategy_version: str | None = None
    feature_version: str

    observations_total: int | None = None
    observations_last_hour: int | None = None
    signals_total: int | None = None
    signals_last_hour: int | None = None
    tokens_with_a_signal: int | None = None
    signal_rate: float | None = Field(
        default=None,
        description="Signals per observation. The denominator is the point.",
    )
    last_signal_at: datetime | None = None

    disclaimer: str = Field(
        default=(
            "Research signals only. No orders are produced, and nothing here "
            "asserts the hypothesis is profitable -- that is unmeasured until "
            "Phase 7 and may well be false."
        ),
        description="Present so a signal count is never read as a profit claim.",
    )


class PaperStatus(ComponentStatus):
    """Paper-trading state. Simulated fills only -- see disclaimer."""

    balance_sol: float | None = None
    equity_sol: float | None = None
    open_positions: int | None = None
    trades_total: int | None = None

    disclaimer: str = Field(
        default=(
            "Paper trading only. No signer, no wallet, no RPC -- fills are "
            "simulated against recorded market data, never a real order."
        ),
        description="Present so a balance is never read as real money.",
    )


class OutcomeStatus(ComponentStatus):
    """Outcome-labelling state (spec §15-16)."""

    outcomes_total: int | None = None
    outcomes_final: int | None = None
    observations_pending: int | None = Field(
        default=None,
        description="Observations with no final label yet -- what the next pass still owes.",
    )

    signalled_final_count: int | None = Field(
        default=None,
        description=(
            "Observations that both fired a signal and have a final outcome. This is "
            "the only number that says whether EARLY MOMENTUM can be evaluated yet -- "
            "`signals_total` counts signals whose outcome may still be unresolved."
        ),
    )
    research_ready_threshold: int | None = None
    research_ready: bool | None = Field(
        default=None,
        description=(
            "Whether signalled_final_count has reached the threshold. Surfaced because "
            "it was previously observable only as a one-off Discord ping: a run that "
            "crossed the threshold while the webhook was unset, or that had already "
            "announced before a restart, left no way to ask."
        ),
    )


class StatusResponse(BaseModel):
    """Measured system state.

    ``phase`` is part of the payload so a reader can tell at a glance which
    counters are *expected* to be missing.
    """

    status: Literal["healthy", "degraded"]
    version: str
    environment: str
    phase: int
    uptime_seconds: float
    database: DatabaseStatus
    trading_mode: Literal["paper"] = "paper"
    is_live: Literal[False] = False

    tokens_discovered: int | None = Field(
        default=None,
        description="Rows in `tokens`. Null when the database is unreachable — never 0.",
    )
    tokens_tracking: int | None = Field(
        default=None,
        description="Tokens in state TRACKING. Null when the database is unreachable.",
    )
    discovery: DiscoveryStatus
    tracking: TrackingStatus
    signals: SignalStatus
    paper: PaperStatus
    outcomes: OutcomeStatus

    stats_age_seconds: float | None = Field(
        default=None,
        description=(
            "How old the database aggregates in this payload are. They are refreshed on a "
            "clock rather than per request, because computing them inline measured at ~14s. "
            "Null means the first refresh has not completed -- not that they are zero."
        ),
    )

    not_implemented: list[str] = Field(
        default_factory=list,
        description="Metrics deliberately absent because their phase has not been built.",
    )
