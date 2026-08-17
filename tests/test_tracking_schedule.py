"""The adaptive schedule, and the capacity arithmetic that constrains it."""

from __future__ import annotations

import pytest

from hades.tracking.schedule import TrackingSchedule, TrackingTier

SCHEDULE = TrackingSchedule()


@pytest.mark.parametrize(
    ("age", "tier"),
    [
        (0.0, TrackingTier.EARLY),
        (299.9, TrackingTier.EARLY),
        (300.0, TrackingTier.MEDIUM),
        (1799.9, TrackingTier.MEDIUM),
        (1800.0, TrackingTier.NORMAL),
        (3599.9, TrackingTier.NORMAL),
        (3600.0, TrackingTier.RETIRED),
        (86_400.0, TrackingTier.RETIRED),
    ],
)
def test_tier_boundaries(age: float, tier: TrackingTier) -> None:
    assert SCHEDULE.tier_for(age) is tier


@pytest.mark.parametrize(
    ("age", "interval"),
    [(0.0, 10.0), (299.0, 10.0), (300.0, 30.0), (1800.0, 60.0)],
)
def test_intervals_match_the_spec_defaults(age: float, interval: float) -> None:
    assert SCHEDULE.interval_for(age) == interval


def test_a_retired_token_has_no_next_interval() -> None:
    """None, not a large number. There is no next snapshot, not a distant one."""
    assert SCHEDULE.interval_for(3600.0) is None


def test_the_long_tier_is_reachable_when_the_horizon_allows_it() -> None:
    """With the default 1h horizon the LONG tier never applies.

    Worth pinning: a tier that silently never fires is the kind of dead
    configuration that looks fine in review.
    """
    assert SCHEDULE.tier_for(7300.0) is TrackingTier.RETIRED

    full_day = TrackingSchedule(retire_after_seconds=86_400.0)
    assert full_day.tier_for(7300.0) is TrackingTier.LONG
    assert full_day.interval_for(7300.0) == 300.0


class TestCapacity:
    def test_one_hour_horizon_costs_110_snapshots(self) -> None:
        assert SCHEDULE.snapshots_per_token() == pytest.approx(110.0)

    def test_the_full_day_schedule_costs_434(self) -> None:
        """The number that makes tracking the whole universe impossible.

        At 0.24-0.55 tokens/s created, 434 snapshots each is 104-239 req/s
        against a primary measured to sustain 1.64.
        """
        full_day = TrackingSchedule(retire_after_seconds=86_400.0)
        assert full_day.snapshots_per_token() == pytest.approx(434.0)

    def test_default_capacity_stays_near_the_measured_budget(self) -> None:
        """40 concurrent tokens should cost about 1.2 req/s.

        This is the assertion that would fail if someone raised the concurrency
        limit without redoing the arithmetic — which is exactly how a system
        walks into an unpublished rate limit.
        """
        rate = SCHEDULE.estimated_requests_per_second(40)
        assert rate == pytest.approx(1.22, abs=0.05)
        assert rate < 1.64

    def test_the_estimate_is_an_average_not_a_peak(self) -> None:
        """A burst of admissions puts every token in EARLY at once.

        40 tokens at one snapshot per 10s is 4 req/s — over the measured
        sustained rate. Request spacing in the service is what keeps that from
        arriving all at once.
        """
        peak = 40 / SCHEDULE.early_interval_seconds
        assert peak == pytest.approx(4.0)
        assert peak > SCHEDULE.estimated_requests_per_second(40)


def test_a_zero_horizon_does_not_divide_by_zero() -> None:
    assert TrackingSchedule(retire_after_seconds=0.0).estimated_requests_per_second(10) == 0.0
