"""Application state shared across requests."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hades.config.settings import Settings
from hades.discovery.scheduler import DiscoveryScheduler
from hades.discovery.service import DiscoveryService

STATE_ATTRIBUTE = "hades"


@dataclass(frozen=True, slots=True)
class AppState:
    """Long-lived resources owned by the application.

    ``discovery_service`` and ``discovery_scheduler`` are None only when
    discovery is disabled by configuration. ``/status`` reports that explicitly
    rather than presenting a disabled collector as an idle one.
    """

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    started_at: datetime
    discovery_service: DiscoveryService | None = None
    discovery_scheduler: DiscoveryScheduler | None = None


def get_state(request: Request) -> AppState:
    """Return the application state attached during startup."""
    return cast(AppState, getattr(request.app.state, STATE_ATTRIBUTE))
