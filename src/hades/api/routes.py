"""``/health`` and ``/status``.

Both read live state on every request. Neither is cached: a cached health
check reports the past, and the past is exactly what you do not want when you
are asking whether something is broken right now.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from hades import __version__
from hades.api.schemas import DatabaseStatus, HealthResponse, StatusResponse
from hades.config import Settings
from hades.db.engine import Database, DatabaseHealth
from hades.db.models import Token, TokenState

router = APIRouter(tags=["observability"])

# Counters whose producing phase does not exist yet. Named explicitly so the
# payload states its own incompleteness instead of implying zeros.
NOT_IMPLEMENTED_METRICS = (
    "snapshots_total",
    "signals_total",
    "paper_trades",
    "provider_status",
)


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


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
) -> StatusResponse:
    db_health = await database.check_health()

    tokens_discovered: int | None = None
    tokens_tracking: int | None = None
    if db_health.connected:
        async with database.session() as session:
            tokens_discovered = await session.scalar(select(func.count()).select_from(Token))
            tokens_tracking = await session.scalar(
                select(func.count()).select_from(Token).where(Token.state == TokenState.TRACKING)
            )

    started_at: float = request.app.state.started_at
    return StatusResponse(
        status="healthy" if db_health.connected else "degraded",
        version=__version__,
        environment=settings.environment,
        phase=0,
        uptime_seconds=round(time.monotonic() - started_at, 3),
        database=_to_schema(db_health),
        tokens_discovered=tokens_discovered,
        tokens_tracking=tokens_tracking,
        not_implemented=list(NOT_IMPLEMENTED_METRICS),
    )
