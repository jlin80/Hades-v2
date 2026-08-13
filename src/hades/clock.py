"""Time handling.

Every timestamp in Hades is timezone-aware UTC. Local time is never used
internally (task.md §14). Ruff's DTZ ruleset enforces this at lint time; this
module exists so there is exactly one place that produces "now".
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def age_ms(since: datetime, *, now: datetime | None = None) -> float:
    """Return the age of ``since`` in milliseconds.

    Raises:
        ValueError: if ``since`` is naive. A naive datetime here means some
            code path lost its timezone, which would silently corrupt every
            latency measurement derived from it.
    """
    if since.tzinfo is None:
        raise ValueError("naive datetime passed to age_ms; all timestamps must be tz-aware UTC")
    reference = now if now is not None else utc_now()
    return (reference - since).total_seconds() * 1000.0
