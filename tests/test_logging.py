from __future__ import annotations

import json
import logging

from hades.logging import JsonFormatter, configure_logging


def _record(**kwargs: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="hades.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token_discovered",
        args=None,
        exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


def test_record_renders_as_one_json_object() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["event"] == "token_discovered"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "hades.test"
    assert payload["timestamp"].endswith("+00:00")


def test_context_is_nested_not_flattened() -> None:
    payload = json.loads(JsonFormatter().format(_record(context={"mint": "abc", "source": "x"})))
    assert payload["context"] == {"mint": "abc", "source": "x"}


def test_exception_is_captured() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_is_idempotent() -> None:
    """Re-configuring must replace the handler, not stack a second one.

    A stacked handler double-prints every line, which reads as double the
    activity in the logs — the kind of wrong number that survives review.
    """
    configure_logging("INFO")
    configure_logging("INFO")
    configure_logging("INFO")
    assert len(logging.getLogger().handlers) == 1
