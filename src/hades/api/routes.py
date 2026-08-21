"""``/health``, ``/status`` and ``/metrics``.

Liveness and the in-process state are read fresh on every request. A cached
health check reports the past, and the past is exactly what you do not want when
you are asking whether something is broken right now.

The **database aggregates** are the exception, and a measured one: computing them
per request took ~14 seconds on CT202, because they are unbounded ``COUNT(*)``
over hundreds of thousands of rows. That made ``/status`` unpollable and a
Prometheus scrape impossible, and it put a table scan on the database the
collectors were writing to every time somebody asked how things were going.

They now come from ``hades.monitoring.stats``, refreshed on a clock, and every
response carries ``stats_age_seconds``. Caching a number is fine; presenting a
cached number as live is not.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from hades import __version__
from hades.api.schemas import (
    DatabaseStatus,
    DiscoveryStatus,
    HealthResponse,
    OutcomeStatus,
    PaperStatus,
    SignalStatus,
    StatusResponse,
    TrackingStatus,
)
from hades.config import Settings
from hades.db.engine import Database, DatabaseHealth
from hades.db.models import TokenState
from hades.discovery.runtime import DiscoveryRuntime
from hades.features.engine import FEATURE_VERSION
from hades.monitoring.prometheus import LoopState, SupervisedRuntime, render
from hades.monitoring.runtime import StatsRuntime
from hades.outcomes.runtime import OutcomeRuntime
from hades.paper.runtime import PaperRuntime
from hades.signals.runtime import SignalRuntime
from hades.tracking.runtime import TrackingRuntime, schedule_from_settings

router = APIRouter(tags=["observability"])

# The last phase whose counters are expected to be present. It was left at 5
# through the Phase 6 and 7 merges, so /status understated the system by two
# phases while reporting paper and outcome counters that only exist at 7.
PHASE = 7

# Counters whose producing phase does not exist yet. Named explicitly so the
# payload states its own incompleteness instead of implying zeros.
NOT_IMPLEMENTED_METRICS: tuple[str, ...] = ()


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_discovery(request: Request) -> DiscoveryRuntime:
    runtime: DiscoveryRuntime = request.app.state.discovery
    return runtime


def get_tracking(request: Request) -> TrackingRuntime:
    runtime: TrackingRuntime = request.app.state.tracking
    return runtime


def get_signals(request: Request) -> SignalRuntime:
    runtime: SignalRuntime = request.app.state.signals
    return runtime


def get_paper(request: Request) -> PaperRuntime:
    runtime: PaperRuntime = request.app.state.paper
    return runtime


def get_outcomes(request: Request) -> OutcomeRuntime:
    runtime: OutcomeRuntime = request.app.state.outcomes
    return runtime


def get_stats(request: Request) -> StatsRuntime:
    runtime: StatsRuntime = request.app.state.stats
    return runtime


def _to_schema(health: DatabaseHealth) -> DatabaseStatus:
    return DatabaseStatus(
        connected=health.connected,
        latency_ms=health.latency_ms,
        error=health.error,
    )


@router.get("/health", response_model=HealthResponse)
async def health(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    db_health = await database.check_health()
    return HealthResponse(
        status="healthy" if db_health.connected else "degraded",
        version=__version__,
        environment=settings.environment,
        database=_to_schema(db_health),
    )


@router.get("/status", response_model=StatusResponse)
async def status(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    discovery: Annotated[DiscoveryRuntime, Depends(get_discovery)],
    tracking: Annotated[TrackingRuntime, Depends(get_tracking)],
    signals: Annotated[SignalRuntime, Depends(get_signals)],
    paper: Annotated[PaperRuntime, Depends(get_paper)],
    outcomes: Annotated[OutcomeRuntime, Depends(get_outcomes)],
    stats: Annotated[StatsRuntime, Depends(get_stats)],
) -> StatusResponse:
    db_health = await database.check_health()

    discovery_status = DiscoveryStatus(
        enabled=settings.discovery_enabled,
        running=discovery.is_running,
        last_error=discovery.last_error,
        counters=discovery.counters,
        supervision=discovery.supervision,
    )

    schedule = schedule_from_settings(settings)
    tracking_status = TrackingStatus(
        enabled=settings.tracking_enabled,
        running=tracking.is_running,
        last_error=tracking.last_error,
        counters=tracking.counters,
        supervision=tracking.supervision,
        max_concurrent=settings.tracking_max_concurrent,
        retire_after_seconds=schedule.retire_after_seconds,
        snapshots_per_token=round(schedule.snapshots_per_token(), 1),
        estimated_requests_per_second=round(
            schedule.estimated_requests_per_second(settings.tracking_max_concurrent), 3
        ),
    )

    signal_status = SignalStatus(
        enabled=settings.signals_enabled,
        running=signals.is_running,
        last_error=signals.last_error,
        counters=signals.counters,
        supervision=signals.supervision,
        strategy=signals.strategy,
        strategy_version=signals.strategy_version,
        feature_version=FEATURE_VERSION,
    )

    paper_status = PaperStatus(
        enabled=settings.paper_trading_enabled,
        running=paper.is_running,
        last_error=paper.last_error,
        counters=paper.counters,
        supervision=paper.supervision,
    )

    outcome_status = OutcomeStatus(
        enabled=settings.outcomes_enabled,
        running=outcomes.is_running,
        last_error=outcomes.last_error,
        counters=outcomes.counters,
        supervision=outcomes.supervision,
    )

    # The expensive aggregates come from the refresher, not from this request.
    # Running them inline measured at ~14s on CT202 and put a table scan on the
    # database the collectors were writing to. `stats_age_seconds` below is how
    # a reader tells a current number from a stale one -- the freshness is
    # reported rather than assumed, which is the part that matters.
    snapshot = stats.service.snapshot if stats.service is not None else None
    tokens_discovered: int | None = None
    tokens_tracking: int | None = None
    if snapshot is not None:
        discovery_stats = snapshot.discovery
        tokens_discovered = discovery_stats.total
        tokens_tracking = discovery_stats.by_state.get(TokenState.TRACKING.value, 0)
        discovery_status = discovery_status.model_copy(
            update={
                "tokens_total": discovery_stats.total,
                "tokens_by_state": discovery_stats.by_state,
                "tokens_with_created_at": discovery_stats.with_created_at,
                "tokens_backfill_exhausted": discovery_stats.backfill_exhausted,
                "last_discovered_at": discovery_stats.last_discovered_at,
                "median_discovery_latency_ms": discovery_stats.median_discovery_latency_ms,
            }
        )

        tracking_stats = snapshot.tracking
        tracking_status = tracking_status.model_copy(
            update={
                "tracking_now": tracking_stats.tracking_now,
                "eligible_waiting": tracking_stats.eligible_waiting,
                "snapshots_total": tracking_stats.snapshots_total,
                "snapshots_last_hour": tracking_stats.snapshots_last_hour,
                "stale_snapshots": tracking_stats.stale_snapshots,
                "tokens_retired": tracking_stats.tokens_retired,
                "tokens_migrated": tracking_stats.tokens_migrated,
                "tokens_dead": tracking_stats.tokens_dead,
                "oldest_due_seconds": tracking_stats.oldest_due_seconds,
            }
        )

        signal_stats = snapshot.signals
        signal_status = signal_status.model_copy(
            update={
                "observations_total": signal_stats.observations_total,
                "observations_last_hour": signal_stats.observations_last_hour,
                "signals_total": signal_stats.signals_total,
                "signals_last_hour": signal_stats.signals_last_hour,
                "tokens_with_a_signal": signal_stats.tokens_with_a_signal,
                "signal_rate": signal_stats.signal_rate,
                "last_signal_at": signal_stats.last_signal_at,
            }
        )

        if snapshot.portfolio is not None:
            paper_status = paper_status.model_copy(
                update={
                    "balance_sol": snapshot.portfolio.balance_sol,
                    "equity_sol": snapshot.portfolio.current_equity_sol,
                    "open_positions": snapshot.portfolio.open_positions,
                    "trades_total": paper.counters.get("filled", 0),
                }
            )

        if snapshot.outcomes:
            threshold = settings.research_ready_threshold
            outcome_status = outcome_status.model_copy(
                update={
                    "outcomes_total": snapshot.outcomes["outcomes_total"],
                    "outcomes_final": snapshot.outcomes["outcomes_final"],
                    "observations_pending": snapshot.outcomes["observations_pending"],
                    "signalled_final_count": snapshot.signalled_final_count,
                    "research_ready_threshold": threshold,
                    "research_ready": snapshot.signalled_final_count >= threshold,
                }
            )

    started_at: float = request.app.state.started_at
    return StatusResponse(
        status="healthy" if db_health.connected else "degraded",
        version=__version__,
        environment=settings.environment,
        phase=PHASE,
        uptime_seconds=round(time.monotonic() - started_at, 3),
        database=_to_schema(db_health),
        tokens_discovered=tokens_discovered,
        tokens_tracking=tokens_tracking,
        discovery=discovery_status,
        tracking=tracking_status,
        signals=signal_status,
        paper=paper_status,
        outcomes=outcome_status,
        stats_age_seconds=(round(snapshot.age_seconds, 3) if snapshot else None),
        not_implemented=list(NOT_IMPLEMENTED_METRICS),
    )


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/plain": {}}}},
)
async def metrics(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    discovery: Annotated[DiscoveryRuntime, Depends(get_discovery)],
    tracking: Annotated[TrackingRuntime, Depends(get_tracking)],
    signals: Annotated[SignalRuntime, Depends(get_signals)],
    paper: Annotated[PaperRuntime, Depends(get_paper)],
    outcomes: Annotated[OutcomeRuntime, Depends(get_outcomes)],
    stats: Annotated[StatsRuntime, Depends(get_stats)],
) -> PlainTextResponse:
    """Prometheus exposition for the CT103 scraper.

    Reads only the cached snapshot and in-process state, so it cannot be slow and
    cannot load the database. An endpoint something hits every 15 seconds must
    not be able to hurt the system it is measuring -- which is precisely what
    scraping ``/status`` would have done.
    """
    db_health = await database.check_health()
    runtimes: dict[str, SupervisedRuntime] = {
        "discovery": discovery,
        "tracking": tracking,
        "signals": signals,
        "paper": paper,
        "outcomes": outcomes,
    }
    started_at: float = request.app.state.started_at
    body = render(
        snapshot=stats.service.snapshot if stats.service is not None else None,
        loop_status={name: LoopState.of(runtime) for name, runtime in runtimes.items()},
        counters={name: runtime.counters for name, runtime in runtimes.items()},
        database_connected=db_health.connected,
        database_latency_ms=db_health.latency_ms,
        uptime_seconds=round(time.monotonic() - started_at, 3),
        research_ready_threshold=settings.research_ready_threshold,
    )
    # The version suffix is what Prometheus' own exporters send.
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")
