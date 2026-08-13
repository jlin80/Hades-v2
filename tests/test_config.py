from __future__ import annotations

import pytest
from pydantic import ValidationError

from hades.config import Settings


def test_sync_driver_is_rejected() -> None:
    """A sync driver in an async engine blocks the event loop.

    This is the failure V1 spent three sessions misattributing to providers,
    so it is a startup error here rather than a runtime mystery.
    """
    with pytest.raises(ValidationError, match="asyncpg"):
        Settings(database_url="postgresql://hades:hades@localhost:5432/hades")


def test_psycopg_driver_is_rejected() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        Settings(database_url="postgresql+psycopg://hades:hades@localhost:5432/hades")


def test_asyncpg_driver_is_accepted() -> None:
    settings = Settings(database_url="postgresql+asyncpg://hades:hades@localhost:5432/hades")
    assert settings.database_url.scheme == "postgresql+asyncpg"


def test_settings_are_frozen() -> None:
    """Config is read-only after construction: no runtime mutation of the truth."""
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production")
