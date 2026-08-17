"""Configuration.

One settings object, loaded from the environment (and ``.env`` in development).
Everything is explicit and typed; there are no implicit defaults for anything
that would change what the system *does* — only for what it *is*.

Phase 0 deliberately has no provider, tracking or strategy settings. Those
arrive with the phase that needs them, per the anti-over-engineering rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["local", "homelab", "test"]


class Settings(BaseSettings):
    """Runtime configuration for the whole process."""

    model_config = SettingsConfigDict(
        env_prefix="HADES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "local"
    log_level: LogLevel = "INFO"

    # PostgreSQL is the single source of truth. The async driver is mandatory:
    # a sync URL here would silently give us a blocking driver inside the
    # event loop, which is the exact failure mode that stalled Hades V1.
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://hades:hades@localhost:5432/hades"),
    )
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_pool_max_overflow: int = Field(default=5, ge=0, le=50)
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0)

    # Container-internal bind; what is actually exposed is a compose concern.
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # --- Discovery (Phase 2) -------------------------------------------------
    # Off by default: starting the API must never start writing to the database
    # as a side effect. The operator turns collection on deliberately.
    discovery_enabled: bool = False
    # The WebSocket is the primary discovery path; polling only backfills what a
    # disconnect lost. 60s is not a throughput lever — Hades V1 raised its poll
    # interval 12x and its intake did not fall, because the rate at which tokens
    # appear on Solana is not set by our clock.
    discovery_poll_interval_seconds: float = Field(default=60.0, gt=0)
    discovery_poll_limit: int = Field(default=50, ge=1, le=200)
    # Tokens per pass whose missing created_at we chase. Deferred rather than
    # fetched on arrival: measured live, 49 of 51 immediate fetches 404'd
    # because the socket announces a mint before pump.fun indexes it.
    discovery_backfill_limit: int = Field(default=50, ge=0, le=200)
    # Attempts before we stop asking about a given mint. Some tokens reach the
    # WebSocket but never enter pump.fun's index at all (external launchpads),
    # and without a budget they occupy the queue forever.
    discovery_backfill_max_attempts: int = Field(default=5, ge=1, le=50)

    # --- Tracking (Phase 3) --------------------------------------------------
    tracking_enabled: bool = False
    # The binding constraint, and it is not a preference. Measured: the primary
    # sustains 1.64 req/s while Pump.fun creates 0.24-0.55 tokens/s, and the
    # spec's schedule costs 434 snapshots per token per day -- tracking every
    # token would need 104-239 req/s, 64x to 146x over capacity. So the system
    # tracks a bounded sample. 40 concurrent tokens over a 1h horizon works out
    # to roughly 1.2 req/s; `/status` reports the estimate so the arithmetic is
    # visible rather than folded into a magic number.
    tracking_max_concurrent: int = Field(default=40, ge=1, le=5000)
    tracking_batch_size: int = Field(default=20, ge=1, le=200)
    tracking_pass_interval_seconds: float = Field(default=2.0, gt=0)
    # Spacing inside a batch. The primary's limit is unpublished and is the
    # largest technical risk in the project; a batch fired at once is the shape
    # most likely to find the ceiling.
    tracking_request_spacing_seconds: float = Field(default=0.2, ge=0)
    # Spec §13: data older than this is stale. Recorded on the snapshot rather
    # than applied at read time, so changing the threshold cannot rewrite what
    # was already observed.
    tracking_stale_after_seconds: float = Field(default=60.0, gt=0)
    tracking_max_snapshot_failures: int = Field(default=3, ge=1, le=20)
    tracking_failure_retry_seconds: float = Field(default=30.0, gt=0)

    # Schedule (spec §8). Exact values configurable, as required.
    early_tracking_seconds: float = Field(default=300.0, gt=0)
    early_snapshot_interval_seconds: float = Field(default=10.0, gt=0)
    medium_tracking_seconds: float = Field(default=1800.0, gt=0)
    medium_snapshot_interval_seconds: float = Field(default=30.0, gt=0)
    normal_tracking_seconds: float = Field(default=7200.0, gt=0)
    normal_snapshot_interval_seconds: float = Field(default=60.0, gt=0)
    long_term_snapshot_interval_seconds: float = Field(default=300.0, gt=0)
    # One hour: covers every outcome horizon spec §15 asks for (return_1m
    # through return_1h) at 110 snapshots per token, which the budget affords
    # for ~785 tokens/day. A 24h horizon costs 4x per token and buys nothing
    # §15 currently asks for.
    tracking_retire_after_seconds: float = Field(default=3600.0, gt=0)

    provider_timeout_seconds: float = Field(default=10.0, gt=0)
    provider_max_attempts: int = Field(default=3, ge=1, le=10)
    provider_max_connections: int = Field(default=10, ge=1, le=100)

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            msg = (
                f"database_url must use the postgresql+asyncpg driver, got {value.scheme!r}. "
                "A sync driver would block the event loop."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _tiers_must_be_ordered(self) -> Settings:
        """Tier boundaries must increase, or a tier silently never applies.

        A MEDIUM boundary below the EARLY one does not error anywhere: it just
        means no token is ever in the medium tier, and the dataset quietly has
        a sampling rate nobody chose.
        """
        if not (
            self.early_tracking_seconds
            < self.medium_tracking_seconds
            < self.normal_tracking_seconds
        ):
            msg = (
                "tracking tier boundaries must increase: "
                f"early={self.early_tracking_seconds} < "
                f"medium={self.medium_tracking_seconds} < "
                f"normal={self.normal_tracking_seconds}"
            )
            raise ValueError(msg)
        return self

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


def load_settings() -> Settings:
    """Build a Settings instance from the environment.

    Not cached: caching a frozen global makes tests lie to each other. Callers
    that need it repeatedly hold it on the application state instead.
    """
    return Settings()
