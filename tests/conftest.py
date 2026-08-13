"""Shared test fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.config.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Port 1 is privileged and nothing listens there, so a connection attempt fails
# immediately rather than hanging until a timeout.
UNREACHABLE_DSN = "postgresql+asyncpg://hades:secret@127.0.0.1:1/hades"


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def settings() -> Settings:
    """Settings built from explicit values.

    Init keyword arguments outrank both the environment and any .env file in
    pydantic-settings, so this fixture is unaffected by the developer's local
    configuration.
    """
    return Settings(
        environment="development",
        database_url=UNREACHABLE_DSN,
        log_format="console",
        log_level="DEBUG",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A test client whose database is deliberately unreachable.

    Creating an async engine does not open a connection, so the application
    starts cleanly and every database probe genuinely fails — which is the
    behaviour the health endpoints must report.
    """
    with TestClient(create_app(settings)) as test_client:
        yield test_client
