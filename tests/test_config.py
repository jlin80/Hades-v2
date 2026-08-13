"""Configuration loading and the documentation/consumption contract."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from hades.config.settings import Settings

# Variables that docker-compose.yml substitutes but the application never reads.
# Adding a name here is a deliberate, reviewable act: it is the only way to
# document a variable that Settings does not consume.
COMPOSE_ONLY_VARIABLES = frozenset(
    {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_PUBLISHED_PORT",
        "API_PUBLISHED_PORT",
        "HADES_IMAGE_TAG",
    }
)

_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _documented_variables(project_root: Path) -> set[str]:
    return set(_ASSIGNMENT.findall((project_root / ".env.example").read_text(encoding="utf-8")))


def test_valid_settings_load(settings: Settings) -> None:
    assert settings.environment == "development"
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_sync_driver_dsn_is_rejected() -> None:
    """A sync DSN would work until the first query, then fail inside the loop."""
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(database_url="postgresql://hades:secret@localhost:5432/hades")


def test_missing_database_url_is_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The process must refuse to start rather than run half-configured."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env to fall back on
    with pytest.raises(ValidationError):
        Settings()


def test_password_is_redacted_for_logging() -> None:
    settings = Settings(database_url="postgresql+asyncpg://hades:sup3rs3cret@db:5432/hades")
    safe = settings.database_url_safe
    assert "sup3rs3cret" not in safe
    assert safe == "postgresql+asyncpg://hades:***@db:5432/hades"


def test_pool_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://a:b@c:5432/d", db_pool_size=0)


def test_every_setting_is_documented(project_root: Path) -> None:
    """A setting that exists but is undocumented is a configuration trap."""
    documented = _documented_variables(project_root)
    undocumented = {name.upper() for name in Settings.model_fields} - documented
    assert not undocumented, f"settings missing from .env.example: {sorted(undocumented)}"


def test_no_documented_variable_is_dead(project_root: Path) -> None:
    """Every documented variable is consumed by the app or by docker compose.

    This is the guard against Hades v1's SCORING_* namespace: documented,
    surfaced in the UI, wired to nothing, silently ignoring operator changes.
    """
    documented = _documented_variables(project_root)
    consumed = {name.upper() for name in Settings.model_fields} | COMPOSE_ONLY_VARIABLES
    dead = documented - consumed
    assert not dead, f"documented but consumed by nothing: {sorted(dead)}"


def test_compose_only_variables_are_actually_used_by_compose(project_root: Path) -> None:
    """Keeps the compose-only allowlist honest as the compose file evolves."""
    compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    unused = {name for name in COMPOSE_ONLY_VARIABLES if name not in compose}
    assert not unused, f"allowlisted but absent from docker-compose.yml: {sorted(unused)}"
