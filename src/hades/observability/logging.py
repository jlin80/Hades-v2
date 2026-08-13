"""Structured logging.

Every log line is a structured event with an ISO-8601 UTC timestamp. In
production the renderer emits JSON so lines are greppable and machine-parseable;
locally a human-readable renderer is available.

Third-party loggers (uvicorn, sqlalchemy, alembic) are routed through the same
pipeline so that the process emits exactly one log format on stdout — Hades v1
had panels showing zeros during a real outage because errors were swallowed by
bare handlers and never made it into a readable stream.
"""

import logging
import sys
from typing import Any

import structlog
from structlog.typing import Processor

from hades.config.settings import LogFormat, LogLevel

# Loggers that install their own handlers and would otherwise double-print.
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "alembic")


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def configure_logging(*, level: LogLevel = "INFO", log_format: LogFormat = "json") -> None:
    """Configure structlog and the stdlib root logger.

    Safe to call more than once; existing root handlers are replaced rather
    than appended, so repeated calls cannot cause duplicated output.
    """
    shared = _shared_processors()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True


def get_logger(name: str, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structured logger."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    if initial_values:
        return logger.bind(**initial_values)
    return logger
