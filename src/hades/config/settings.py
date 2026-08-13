"""Application settings, loaded exclusively from the environment / .env.

Nothing important is hardcoded (task.md §15).

Deliberate scope note
---------------------
This module declares *only* the settings Phase 0 actually consumes. Provider
toggles, discovery intervals and snapshot intervals are absent on purpose: they
arrive in the phase that implements them.

Hades v1 shipped a fully documented ``SCORING_*`` namespace that was rendered in
the dashboard's configuration screen and wired to absolutely nothing — an
operator "tuning" it silently changed no behaviour at all. Config that exists
before its consumer is worse than missing config, because it looks authoritative.

``tests/test_config.py`` enforces both directions: every documented variable is
either consumed here or explicitly declared compose-only, and every setting
declared here is documented in ``.env.example``.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "console"]
Environment = Literal["development", "production"]

# The application speaks to PostgreSQL over asyncpg. A sync driver URL would
# work right up until the first query and then fail inside the event loop, so
# it is rejected at startup instead.
REQUIRED_DB_SCHEME = "postgresql+asyncpg://"


class Settings(BaseSettings):
    """Runtime configuration.

    ``extra="ignore"`` is intentional: the ``.env`` file is shared with Docker
    Compose, which needs ``POSTGRES_*`` for the database container. Those keys
    are not application settings. The dead-config guard lives in the test suite
    rather than here, so that a stray shell variable cannot prevent startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Field(
        default="development",
        description="Deployment environment. Controls default log format only.",
    )

    database_url: str = Field(
        description="Async PostgreSQL DSN. Must use the postgresql+asyncpg scheme.",
    )

    db_pool_size: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of persistent connections held by the SQLAlchemy pool.",
    )
    db_max_overflow: int = Field(
        default=5,
        ge=0,
        le=50,
        description="Connections the pool may open beyond db_pool_size under load.",
    )
    db_connect_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        description="Timeout for establishing a new database connection.",
    )

    log_level: LogLevel = Field(default="INFO", description="Minimum severity emitted.")
    log_format: LogFormat = Field(
        default="json",
        description="'json' for machine-readable logs, 'console' for local development.",
    )

    api_host: str = Field(
        default="0.0.0.0",  # noqa: S104 — binding all interfaces is correct inside a container
        description="Address the API binds to.",
    )
    api_port: int = Field(default=8000, ge=1, le=65535, description="Port the API binds to.")

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith(REQUIRED_DB_SCHEME):
            raise ValueError(
                f"DATABASE_URL must start with {REQUIRED_DB_SCHEME!r}, got {value.split('://')[0]!r}"
            )
        return value

    @property
    def database_url_safe(self) -> str:
        """The DSN with its password redacted, safe to log or expose.

        Hades v1 leaked a Helius API key into container logs (httpx logs full
        URLs including query params) and rendered that log stream in the
        dashboard. Credentials never travel through a log line here.
        """
        scheme, _, remainder = self.database_url.partition("://")
        credentials, at, host = remainder.rpartition("@")
        if not at:
            return self.database_url
        user, _, _ = credentials.partition(":")
        return f"{scheme}://{user}:***@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once.

    Raises:
        pydantic.ValidationError: if configuration is missing or invalid. The
            process must not start with a half-valid configuration.
    """
    return Settings()
