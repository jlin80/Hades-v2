"""UTC discipline (task.md §14)."""

from datetime import UTC, datetime, timedelta

import pytest

from hades.clock import age_ms, utc_now


def test_utc_now_is_timezone_aware_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_age_ms_measures_elapsed_time() -> None:
    reference = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
    earlier = reference - timedelta(milliseconds=1500)
    assert age_ms(earlier, now=reference) == pytest.approx(1500.0)


def test_age_ms_rejects_naive_datetime() -> None:
    """A naive timestamp here means a timezone was lost somewhere upstream.

    Silently assuming UTC would corrupt every latency figure derived from it,
    so this raises rather than guessing.
    """
    with pytest.raises(ValueError, match="naive datetime"):
        age_ms(datetime(2026, 8, 13, 12, 0, 0))  # noqa: DTZ001 — naive on purpose
