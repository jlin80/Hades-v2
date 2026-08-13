"""Health and status endpoints (task.md §11).

Both endpoints report measured facts only. There are no simulated values, no
optimistic defaults, and no fields describing components that do not exist yet.

Why ``/status`` looks sparse
----------------------------
task.md §11 sketches a response containing ``tokens_discovered``,
``tokens_tracked``, ``snapshots_collected`` and a ``providers`` map. Phase 0 has
no discovery service, no snapshot table and no providers, so those keys are
absent rather than zero. Reporting ``"providers": {"primary": "healthy"}`` with
no provider wired would be precisely the fabricated telemetry task.md §20
forbids — and precisely the failure Hades v1 shipped, where 29 API handlers
returned empty results on error with no logging, leaving every dashboard panel
showing zeros with no way to distinguish "idle" from "broken".

``pending_capabilities`` names what is missing and which phase adds it, so the
gap is legible instead of looking like an empty system.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from hades import __version__
from hades.api.state import AppState, get_state
from hades.clock import utc_now
from hades.database.engine import get_migration_revision, probe_database

router = APIRouter(tags=["observability"])

HealthState = Literal["healthy", "unhealthy"]

StateDependency = Annotated[AppState, Depends(get_state)]

# Capabilities that do not exist yet, with the phase that introduces them.
PENDING_CAPABILITIES: tuple[str, ...] = (
    "token_discovery (phase 1)",
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
    pending_capabilities: list[str] = Field(
        description="Capabilities not yet implemented. Absent from this response by design."
    )


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
        phase=0,
        started_at=state.started_at,
        uptime_seconds=round((now - state.started_at).total_seconds(), 3),
        database=DatabaseStatus(
            connected=probe.connected,
            latency_ms=probe.latency_ms,
            migration_revision=revision,
            error=probe.error,
        ),
        pending_capabilities=list(PENDING_CAPABILITIES),
    )
