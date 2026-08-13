"""FastAPI application factory and lifecycle."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx2
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hades import __version__
from hades.api.routes import health
from hades.api.state import STATE_ATTRIBUTE, AppState
from hades.clock import utc_now
from hades.config.settings import Settings, get_settings
from hades.database.engine import create_engine, create_session_factory
from hades.discovery.http import create_http_client
from hades.discovery.providers.base import TokenDiscoveryProvider
from hades.discovery.providers.dexscreener import DexScreenerProvider
from hades.discovery.providers.geckoterminal import GeckoTerminalProvider
from hades.discovery.scheduler import DiscoveryScheduler
from hades.discovery.service import DiscoveryService
from hades.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


def build_providers(settings: Settings, client: httpx2.AsyncClient) -> list[TokenDiscoveryProvider]:
    """Return the enabled providers, primary first.

    Order is the fallback order: the second is tried only when the first fails.
    """
    providers: list[TokenDiscoveryProvider] = []
    if settings.primary_provider_enabled:
        providers.append(
            GeckoTerminalProvider(
                client=client,
                base_url=settings.geckoterminal_base_url,
                max_retries=settings.max_retries,
            )
        )
    if settings.fallback_provider_enabled:
        providers.append(
            DexScreenerProvider(
                client=client,
                base_url=settings.dexscreener_base_url,
                max_retries=settings.max_retries,
            )
        )
    return providers


def build_discovery(
    settings: Settings,
    client: httpx2.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[DiscoveryService, DiscoveryScheduler]:
    """Assemble the discovery service and its scheduler."""
    service = DiscoveryService(
        providers=build_providers(settings, client),
        session_factory=session_factory,
    )
    scheduler = DiscoveryScheduler(service, settings.discovery_interval_seconds)
    return service, scheduler


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan handler bound to ``settings``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(level=settings.log_level, log_format=settings.log_format)

        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        client = create_http_client(settings)

        service: DiscoveryService | None = None
        scheduler: DiscoveryScheduler | None = None
        if settings.discovery_enabled:
            service, scheduler = build_discovery(settings, client, session_factory)

        state = AppState(
            settings=settings,
            engine=engine,
            session_factory=session_factory,
            started_at=utc_now(),
            discovery_service=service,
            discovery_scheduler=scheduler,
        )
        setattr(app.state, STATE_ATTRIBUTE, state)

        # The DSN is logged with its password redacted, never raw.
        logger.info(
            "application_started",
            version=__version__,
            environment=settings.environment,
            database=settings.database_url_safe,
            phase=1,
            discovery_enabled=settings.discovery_enabled,
            providers=[provider.name for provider in (service.providers if service else [])],
        )

        if scheduler is not None:
            scheduler.start()

        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            await client.aclose()
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
        summary="Solana memecoin data collection platform (phase 1: token discovery).",
        lifespan=_build_lifespan(resolved),
    )
    app.include_router(health.router)
    return app
