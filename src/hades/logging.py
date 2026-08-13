"""Structured logging.

JSON lines on stdout, stdlib only. No structlog: an extra dependency buys
formatting sugar we do not need, and every wheel we skip is a wheel that
cannot fail to build on the homelab's Bobcat CPU.

Use it as ``logger.info("event_name", extra={"context": {...}})`` — the
``context`` mapping is merged into the record, so log lines stay greppable by
key rather than by prose.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
    # uvicorn attaches an ANSI-coloured duplicate of the message; it is noise
    # in a JSON line and makes every record carry escape sequences.
    "color_message",
}


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload["context"] = context

        # Anything else passed via extra= lands at the top level.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "context":
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter as the only root handler.

    Idempotent: repeated calls replace the handler rather than stacking them,
    so a re-imported module cannot start double-printing every line.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own colourised handlers; make them delegate to ours
    # so the container emits one format, not three.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
