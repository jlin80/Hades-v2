"""FastAPI application factory and lifecycle."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from hades import __version__
from hades.api.routes import health
from hades.api.state import STATE_ATTRIBUTE, AppState
from hades.clock import utc_now
from hades.config.settings import Settings, get_settings
from hades.database.engine import create_engine, create_session_factory
from hades.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan handler bound to ``settings``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(level=settings.log_level, log_format=settings.log_format)

        engine = create_engine(settings)
        state = AppState(
            settings=settings,
            engine=engine,
            session_factory=create_session_factory(engine),
            started_at=utc_now(),
        )
        setattr(app.state, STATE_ATTRIBUTE, state)

        # The DSN is logged with its password redacted, never raw.
        logger.info(
            "application_started",
            version=__version__,
            environment=settings.environment,
            database=settings.database_url_safe,
            phase=0,
        )
        try:
            yield
        finally:
            await engine.dispose()
            logger.info("application_stopped", version=__version__)

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Injected configuration. Defaults to the process settings; tests
            pass their own rather than mutating the environment.
    """
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title="Hades V2",
        version=__version__,
        summary="Solana memecoin data collection platform (phase 0: foundation).",
        lifespan=_build_lifespan(resolved),
    )
    app.include_router(health.router)
    return app
