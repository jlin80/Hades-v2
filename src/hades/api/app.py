"""Application factory — the single composition root.

Everything the process needs is built here and hung on ``app.state``. There is
no module-level engine and no global settings singleton: those are what make a
test suite quietly share state with production code.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hades import __version__
from hades.api.routes import router
from hades.config import Settings, load_settings
from hades.db.engine import Database
from hades.discovery.runtime import DiscoveryRuntime, build_service
from hades.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    ``settings`` is injectable so tests can construct an app without touching
    the ambient environment.
    """
    resolved = settings or load_settings()
    configure_logging(resolved.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        database = Database(resolved)
        app.state.database = database
        app.state.started_at = time.monotonic()

        health = await database.check_health()
        logger.info(
            "startup",
            extra={
                "context": {
                    "version": __version__,
                    "environment": resolved.environment,
                    "database_connected": health.connected,
                    "database_error": health.error,
                    "discovery_enabled": resolved.discovery_enabled,
                }
            },
        )

        # Discovery only starts when the operator asked for it *and* the
        # database answered. Starting a writer against an unreachable database
        # would burn the primary's rate limit producing nothing.
        service = (
            build_service(database, resolved)
            if resolved.discovery_enabled and health.connected
            else None
        )
        if resolved.discovery_enabled and not health.connected:
            logger.error("discovery_not_started_database_unreachable")
        discovery = DiscoveryRuntime(service)
        app.state.discovery = discovery
        await discovery.start()

        # A dead database is logged, not fatal: the process must stay up so
        # /health can *report* the outage. A crash loop reports nothing.
        try:
            yield
        finally:
            await discovery.stop()
            await database.close()
            logger.info("shutdown")

    app = FastAPI(
        title="Hades V2",
        version=__version__,
        summary="Quantitative research system for Pump.fun tokens. Paper trading only.",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app
