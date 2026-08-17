"""``/health`` and ``/status``.

Both read live state on every request. Neither is cached: a cached health
check reports the past, and the past is exactly what you do not want when you
are asking whether something is broken right now.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from hades import __version__
from hades.api.schemas import (
    DatabaseStatus,
    DiscoveryStatus,
    HealthResponse,
    StatusResponse,
    TrackingStatus,
)
from hades.config import Settings
from hades.db.engine import Database, DatabaseHealth
from hades.db.models import TokenState
from hades.discovery.repository import TokenRepository
from hades.discovery.runtime import DiscoveryRuntime
from hades.tracking.repository import TrackingRepository
from hades.tracking.runtime import TrackingRuntime, schedule_from_settings

router = APIRouter(tags=["observability"])

# Counters whose producing phase does not exist yet. Named explicitly so the
# payload states its own incompleteness instead of implying zeros.
NOT_IMPLEMENTED_METRICS = (
    "signals_total",
    "paper_trades",
)


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
) -> StatusResponse:
    db_health = await database.check_health()

    discovery_status = DiscoveryStatus(
        enabled=settings.discovery_enabled,
        running=discovery.is_running,
        last_error=discovery.last_error,
        counters=discovery.counters,
    )

    schedule = schedule_from_settings(settings)
    tracking_status = TrackingStatus(
        enabled=settings.tracking_enabled,
        running=tracking.is_running,
        last_error=tracking.last_error,
        counters=tracking.counters,
        max_concurrent=settings.tracking_max_concurrent,
        retire_after_seconds=schedule.retire_after_seconds,
        snapshots_per_token=round(schedule.snapshots_per_token(), 1),
        estimated_requests_per_second=round(
            schedule.estimated_requests_per_second(settings.tracking_max_concurrent), 3
        ),
    )

    tokens_discovered: int | None = None
    tokens_tracking: int | None = None
    if db_health.connected:
        async with database.session() as session:
            repository = TokenRepository(session)
            stats = await repository.stats(
                backfill_max_attempts=settings.discovery_backfill_max_attempts
            )
            tokens_discovered = stats.total
            tokens_tracking = stats.by_state.get(TokenState.TRACKING.value, 0)
            discovery_status = discovery_status.model_copy(
                update={
                    "tokens_total": stats.total,
                    "tokens_by_state": stats.by_state,
                    "tokens_with_created_at": stats.with_created_at,
                    "tokens_backfill_exhausted": stats.backfill_exhausted,
                    "last_discovered_at": stats.last_discovered_at,
                    "median_discovery_latency_ms": stats.median_discovery_latency_ms,
                }
            )

            tracking_stats = await TrackingRepository(session, schedule).stats()
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

    started_at: float = request.app.state.started_at
    return StatusResponse(
        status="healthy" if db_health.connected else "degraded",
        version=__version__,
        environment=settings.environment,
        phase=3,
        uptime_seconds=round(time.monotonic() - started_at, 3),
        database=_to_schema(db_health),
        tokens_discovered=tokens_discovered,
        tokens_tracking=tokens_tracking,
        discovery=discovery_status,
        tracking=tracking_status,
        not_implemented=list(NOT_IMPLEMENTED_METRICS),
    )
