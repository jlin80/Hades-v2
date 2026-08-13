"""Response models for the observability endpoints.

Every field here is something the process actually measured. There is no
placeholder counter: a metric whose producing phase does not exist yet is
absent from the schema, not present as a zero. A zero that means "not built"
is indistinguishable from a zero that means "nothing happened", and the second
is a finding while the first is noise.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DatabaseStatus(BaseModel):
    connected: bool
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """Liveness plus the one dependency we cannot work without."""

    status: Literal["healthy", "degraded"]
    version: str
    environment: str
    database: DatabaseStatus
    trading_mode: Literal["paper"] = "paper"
    is_live: Literal[False] = False


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

    not_implemented: list[str] = Field(
        default_factory=list,
        description="Metrics deliberately absent because their phase has not been built.",
    )
