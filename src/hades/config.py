"""Configuration.

One settings object, loaded from the environment (and ``.env`` in development).
Everything is explicit and typed; there are no implicit defaults for anything
that would change what the system *does* — only for what it *is*.

Phase 0 deliberately has no provider, tracking or strategy settings. Those
arrive with the phase that needs them, per the anti-over-engineering rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
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

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


def load_settings() -> Settings:
    """Build a Settings instance from the environment.

    Not cached: caching a frozen global makes tests lie to each other. Callers
    that need it repeatedly hold it on the application state instead.
    """
    return Settings()
