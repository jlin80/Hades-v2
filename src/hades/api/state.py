"""Application state shared across requests."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hades.config.settings import Settings

STATE_ATTRIBUTE = "hades"


@dataclass(frozen=True, slots=True)
class AppState:
    """Long-lived resources owned by the application."""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    started_at: datetime


def get_state(request: Request) -> AppState:
    """Return the application state attached during startup."""
    return cast(AppState, getattr(request.app.state, STATE_ATTRIBUTE))
