"""Structured logging behaviour."""

import json
import logging
from collections.abc import Iterator

import pytest

from hades.observability.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Leave the root logger clean for other tests."""
    yield
    logging.getLogger().handlers.clear()


def test_json_logs_are_parseable_with_utc_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="json")
    get_logger("test").info("token_discovered", token_address="So111", provider="pumpfun")

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "token_discovered"
    assert payload["level"] == "info"
    assert payload["token_address"] == "So111"
    assert payload["provider"] == "pumpfun"
    # structlog's ISO timestamper emits UTC with a trailing Z.
    assert payload["timestamp"].endswith("Z")


def test_repeated_configuration_does_not_duplicate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Double-configuring must replace handlers, never stack them."""
    configure_logging(level="INFO", log_format="json")
    configure_logging(level="INFO", log_format="json")
    get_logger("test").info("only_once")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1


def test_level_filtering_is_applied(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", log_format="json")
    logger = get_logger("test")
    logger.info("suppressed")
    logger.warning("emitted")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "emitted"


def test_stdlib_loggers_are_routed_through_the_same_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """uvicorn/sqlalchemy must not emit a second, unstructured format."""
    configure_logging(level="INFO", log_format="json")
    logging.getLogger("uvicorn.error").warning("third party line")

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "third party line"
    assert payload["logger"] == "uvicorn.error"
