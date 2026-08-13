"""Token discovery: fetch, validate, store, deduplicate.

The flow is ONE PRIMARY SOURCE -> ONE FALLBACK SOURCE -> DATABASE (task.md §6).
The fallback is tried only when the primary fails, and a provider is marked
healthy only after an attempt actually succeeded.

Provider health starts as "unknown", never "healthy". task.md §20 forbids
marking a provider healthy without checking it, and v1's dashboards were
misleading precisely because optimistic defaults were indistinguishable from
measured success.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hades.clock import utc_now
from hades.discovery.errors import ProviderError
from hades.discovery.models import DiscoveryRun
from hades.discovery.providers.base import TokenDiscoveryProvider
from hades.discovery.repository import insert_new_tokens
from hades.discovery.validation import validate_batch
from hades.observability.logging import get_logger

logger = get_logger(__name__)

HealthStatus = Literal["unknown", "healthy", "failed"]


@dataclass
class ProviderHealth:
    """Measured state of one provider. Never assumed."""

    name: str
    status: HealthStatus = "unknown"
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    def record_success(self) -> None:
        self.status = "healthy"
        self.last_success_at = utc_now()
        self.last_error = None
        self.consecutive_failures = 0

    def record_failure(self, error: str) -> None:
        self.status = "failed"
        self.last_failure_at = utc_now()
        self.last_error = error
        self.consecutive_failures += 1


@dataclass
class DiscoveryService:
    """Runs one discovery cycle across the configured providers."""

    providers: list[TokenDiscoveryProvider]
    session_factory: async_sessionmaker[AsyncSession]
    health: dict[str, ProviderHealth] = field(default_factory=dict)
    last_run: DiscoveryRun | None = None

    def __post_init__(self) -> None:
        for provider in self.providers:
            self.health.setdefault(provider.name, ProviderHealth(name=provider.name))

    async def run_once(self) -> DiscoveryRun:
        """Discover, validate and store tokens. Returns the measured outcome."""
        started_at = utc_now()

        if not self.providers:
            return self._finish(started_at, None, 0, 0, 0, 0, 0, "no discovery provider is enabled")

        for provider in self.providers:
            health = self.health[provider.name]
            try:
                discovered = await provider.discover_tokens()
            except ProviderError as exc:
                # Full attribution: provider, endpoint, error type, status code,
                # retry count (task.md §6). Never swallowed.
                #
                # TRY400 asks for logger.exception. The traceback is omitted on
                # purpose: ProviderError already carries every diagnostic field,
                # its stack is always the same retry machinery, and a provider
                # outage repeats this line every discovery interval. A traceback
                # each time would bury the fields that actually identify the
                # fault.
                health.record_failure(str(exc))
                logger.error("discovery_provider_failed", **exc.as_log_fields())  # noqa: TRY400
                continue

            health.record_success()

            result = validate_batch(discovered)
            for rejection in result.rejections:
                logger.warning(
                    "token_rejected",
                    provider=rejection.provider_name,
                    token_address=rejection.token_address,
                    reason=str(rejection.reason),
                    detail=rejection.detail,
                )

            async with self.session_factory() as session:
                inserted = await insert_new_tokens(session, result.valid)

            duplicates = len(result.valid) - inserted
            run = self._finish(
                started_at,
                provider.name,
                fetched=len(discovered),
                valid=len(result.valid),
                rejected=len(result.rejections),
                inserted=inserted,
                duplicates=duplicates,
                error=None,
            )
            logger.info(
                "discovery_run_completed",
                provider=provider.name,
                fetched=run.fetched,
                valid=run.valid,
                rejected=run.rejected,
                inserted=run.inserted,
                duplicates=run.duplicates,
                duration_ms=round(run.duration_ms, 1),
            )
            return run

        error = "all discovery providers failed"
        logger.error(
            "discovery_run_failed",
            error=error,
            providers=[provider.name for provider in self.providers],
        )
        return self._finish(started_at, None, 0, 0, 0, 0, 0, error)

    def _finish(
        self,
        started_at: datetime,
        provider_name: str | None,
        fetched: int,
        valid: int,
        rejected: int,
        inserted: int,
        duplicates: int,
        error: str | None,
    ) -> DiscoveryRun:
        run = DiscoveryRun(
            provider_name=provider_name,
            started_at=started_at,
            finished_at=utc_now(),
            fetched=fetched,
            valid=valid,
            rejected=rejected,
            inserted=inserted,
            duplicates=duplicates,
            error=error,
        )
        self.last_run = run
        return run
