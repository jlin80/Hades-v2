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


class HealthResponse(BaseModel):
    """Liveness plus the one dependency we cannot work without."""

    status: Literal["healthy", "degraded"]
    version: str
    environment: str
    database: DatabaseStatus
    trading_mode: Literal["paper"] = "paper"
    is_live: Literal[False] = False


class DiscoveryStatus(BaseModel):
    """Discovery state, all of it measured.

    ``running`` is whether the background task is actually alive, not whether
    it was configured to start. Hades V1's dashboard reported healthy components
    that were doing nothing; the distinction is the whole point.
    """

    enabled: bool
    running: bool
    last_error: str | None = None
    counters: dict[str, int] = Field(default_factory=dict)

    tokens_total: int | None = None
    tokens_by_state: dict[str, int] | None = None
    tokens_with_created_at: int | None = None
    last_discovered_at: datetime | None = None
    median_discovery_latency_ms: float | None = Field(
        default=None,
        description=(
            "Median of (discovered_at - created_at). How far behind creation our "
            "first sighting is — the reason those are two columns and not one."
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

    not_implemented: list[str] = Field(
        default_factory=list,
        description="Metrics deliberately absent because their phase has not been built.",
    )
