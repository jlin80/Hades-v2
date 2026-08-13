"""Health and status endpoints (task.md §11).

Both endpoints report measured facts only. There are no simulated values, no
optimistic defaults, and no fields describing components that do not exist yet.

What appears here grows with the phases. Phase 1 adds real token counts, a real
last-discovery timestamp read from the database, and real per-provider health
derived from actual attempts. Snapshot and outcome metrics are still absent
because there is still nothing collecting them.

Hades v1 shipped 29 API handlers that caught exceptions and returned empty
results with no logging, so during a genuine outage every dashboard panel showed
zeros and nothing distinguished "idle" from "broken". Every number below is
either measured or explicitly null.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from hades import __version__
from hades.api.state import AppState, get_state
from hades.clock import utc_now
from hades.database.engine import get_migration_revision, probe_database
from hades.discovery.repository import count_tokens, latest_discovery_at

router = APIRouter(tags=["observability"])

HealthState = Literal["healthy", "unhealthy"]

# Capabilities that do not exist yet, with the phase that introduces them.
PENDING_CAPABILITIES: tuple[str, ...] = (
    "market_snapshots (phase 2)",
    "outcome_tracking (phase 3)",
    "data_validation_report (phase 4)",
)


class DatabaseStatus(BaseModel):
    """Measured database state."""

    connected: bool = Field(description="Result of a real SELECT 1 against PostgreSQL.")
    latency_ms: float | None = Field(description="Round-trip latency, null when the probe failed.")
    migration_revision: str | None = Field(
        description="Current Alembic revision, null if migrations were never applied."
    )
    error: str | None = Field(description="Exception type and message when the probe failed.")


class ProviderStatus(BaseModel):
    """Measured provider state. 'unknown' until an attempt has actually run."""

    status: Literal["unknown", "healthy", "failed"]
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    consecutive_failures: int


class LastRunStatus(BaseModel):
    """Outcome of the most recent discovery cycle in this process."""

    provider: str | None
    finished_at: datetime
    duration_ms: float
    fetched: int
    valid: int
    rejected: int
    inserted: int
    duplicates: int
    error: str | None


class DiscoveryStatus(BaseModel):
    """Token discovery state (phase 1)."""

    enabled: bool
    scheduler_running: bool
    interval_seconds: float
    tokens_discovered: int | None = Field(
        description="Total tokens stored. Null when the database is unreachable, never a guess."
    )
    last_discovery_at: datetime | None = Field(
        description="Most recent discovery, read from the database so it survives a restart."
    )
    last_run: LastRunStatus | None = Field(
        description="Null until a cycle has completed in this process."
    )
    providers: dict[str, ProviderStatus]


class HealthResponse(BaseModel):
    """Minimal liveness/readiness answer, cheap enough for a container probe."""

    status: HealthState
    timestamp: datetime
    version: str


class StatusResponse(BaseModel):
    """Operational detail behind the health verdict."""

    status: HealthState
    version: str
    environment: str
    phase: int = Field(description="Development phase currently implemented.")
    started_at: datetime
    uptime_seconds: float
    database: DatabaseStatus
    discovery: DiscoveryStatus
    pending_capabilities: list[str] = Field(
        description="Capabilities not yet implemented. Absent from this response by design."
    )


StateDependency = Annotated[AppState, Depends(get_state)]


@router.get("/health", response_model=HealthResponse, summary="Liveness and readiness")
async def health(response: Response, state: StateDependency) -> HealthResponse:
    """Report whether the process can serve requests backed by its database.

    Returns HTTP 503 when the database is unreachable so that container
    orchestration and load balancers observe the real state.
    """
    probe = await probe_database(state.engine)
    if not probe.connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="healthy" if probe.connected else "unhealthy",
        timestamp=utc_now(),
        version=__version__,
    )


@router.get("/status", response_model=StatusResponse, summary="Operational metrics")
async def get_status(response: Response, state: StateDependency) -> StatusResponse:
    """Report real operational metrics for the phase currently implemented."""
    probe = await probe_database(state.engine)
    revision = await get_migration_revision(state.engine) if probe.connected else None

    if not probe.connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    now = utc_now()
    return StatusResponse(
        status="healthy" if probe.connected else "unhealthy",
        version=__version__,
        environment=state.settings.environment,
        phase=1,
        started_at=state.started_at,
        uptime_seconds=round((now - state.started_at).total_seconds(), 3),
        database=DatabaseStatus(
            connected=probe.connected,
            latency_ms=probe.latency_ms,
            migration_revision=revision,
            error=probe.error,
        ),
        discovery=await _discovery_status(state, database_available=probe.connected),
        pending_capabilities=list(PENDING_CAPABILITIES),
    )


async def _discovery_status(state: AppState, *, database_available: bool) -> DiscoveryStatus:
    """Build the discovery section from measured state."""
    service = state.discovery_service
    scheduler = state.discovery_scheduler

    tokens_discovered: int | None = None
    last_discovery: datetime | None = None
    if database_available:
        # Counts come from the database, not from a process counter, so they are
        # correct after a restart (task.md §12).
        async with state.session_factory() as session:
            tokens_discovered = await count_tokens(session)
            last_discovery = await latest_discovery_at(session)

    providers = {
        name: ProviderStatus(
            status=health_state.status,
            last_success_at=health_state.last_success_at,
            last_failure_at=health_state.last_failure_at,
            last_error=health_state.last_error,
            consecutive_failures=health_state.consecutive_failures,
        )
        for name, health_state in (service.health.items() if service else {}.items())
    }

    last_run: LastRunStatus | None = None
    if service is not None and service.last_run is not None:
        run = service.last_run
        last_run = LastRunStatus(
            provider=run.provider_name,
            finished_at=run.finished_at,
            duration_ms=round(run.duration_ms, 1),
            fetched=run.fetched,
            valid=run.valid,
            rejected=run.rejected,
            inserted=run.inserted,
            duplicates=run.duplicates,
            error=run.error,
        )

    return DiscoveryStatus(
        enabled=state.settings.discovery_enabled,
        scheduler_running=scheduler.running if scheduler else False,
        interval_seconds=state.settings.discovery_interval_seconds,
        tokens_discovered=tokens_discovered,
        last_discovery_at=last_discovery,
        last_run=last_run,
        providers=providers,
    )
