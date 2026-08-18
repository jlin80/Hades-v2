"""Outcome labelling: returns, MFE/MAE, and the triple barrier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hades.features.series import Observation, SnapshotSeries
from hades.outcomes.labels import BarrierConfig, BarrierLabel, Outcome, compute_outcome

T0 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
CONFIG = BarrierConfig(name="tp30_sl20_1h")


def observation(offset: float, price: float | None) -> Observation:
    return Observation(observed_at=T0 + timedelta(seconds=offset), price_sol=price)


def series(*points: tuple[float, float | None]) -> SnapshotSeries:
    return SnapshotSeries([observation(offset, price) for offset, price in points])


def label(
    *points: tuple[float, float | None],
    config: BarrierConfig = CONFIG,
    now_offset: float = 7200.0,
) -> Outcome | None:
    return compute_outcome(
        series(*points),
        at=T0,
        config=config,
        now=T0 + timedelta(seconds=now_offset),
    )


class TestTripleBarrier:
    def test_upper_barrier(self) -> None:
        """Spec §15's question: did +30% happen before -20%?"""
        outcome = label((0.0, 1.0), (60.0, 1.1), (120.0, 1.35))
        assert outcome is not None
        assert outcome.label is BarrierLabel.UPPER
        assert outcome.barrier_hit_at_seconds == 120.0
        assert outcome.is_final

    def test_lower_barrier(self) -> None:
        outcome = label((0.0, 1.0), (60.0, 0.9), (120.0, 0.75))
        assert outcome is not None
        assert outcome.label is BarrierLabel.LOWER
        assert outcome.barrier_hit_at_seconds == 120.0

    def test_timeout_when_neither_is_touched(self) -> None:
        outcome = label((0.0, 1.0), (600.0, 1.05), (3000.0, 0.95))
        assert outcome is not None
        assert outcome.label is BarrierLabel.TIMEOUT
        assert outcome.barrier_hit_at_seconds is None
        assert outcome.is_final

    def test_the_first_barrier_touched_wins(self) -> None:
        """Not the largest excursion -- the first one."""
        outcome = label((0.0, 1.0), (60.0, 0.75), (120.0, 2.0))
        assert outcome is not None
        assert outcome.label is BarrierLabel.LOWER

    def test_lower_wins_when_one_snapshot_satisfies_both(self) -> None:
        """Same conservatism as the exit rules.

        Between two snapshots the price may have touched both barriers and we
        cannot see which came first. Assuming the worse path understates
        results rather than flattering them.
        """
        outcome = label((0.0, 1.0), (60.0, 0.5))
        assert outcome is not None
        assert outcome.label is BarrierLabel.LOWER

    def test_nothing_after_the_time_barrier_counts(self) -> None:
        """A move at 90 minutes cannot label a one-hour barrier."""
        outcome = label((0.0, 1.0), (5400.0, 3.0))
        assert outcome is not None
        assert outcome.label is BarrierLabel.TIMEOUT


class TestUnresolvedIsNotTimeout:
    def test_an_unelapsed_window_is_unresolved(self) -> None:
        """ "Neither barrier was touched" and "we do not know yet" differ.

        Collapsing them fills the dataset with label noise that looks like
        evidence, and it is worst for the newest tokens -- exactly the ones the
        research is about.
        """
        outcome = label((0.0, 1.0), (60.0, 1.02), now_offset=120.0)
        assert outcome is not None
        assert outcome.label is BarrierLabel.UNRESOLVED
        assert outcome.is_final is False

    def test_a_barrier_touch_is_final_even_early(self) -> None:
        outcome = label((0.0, 1.0), (60.0, 1.4), now_offset=120.0)
        assert outcome is not None
        assert outcome.label is BarrierLabel.UPPER
        assert outcome.is_final


class TestReturns:
    def test_returns_at_each_elapsed_horizon(self) -> None:
        outcome = label((0.0, 1.0), (60.0, 1.1), (300.0, 1.2), (3600.0, 0.9))
        assert outcome is not None
        assert outcome.returns["1m"] == pytest.approx(0.1)
        assert outcome.returns["5m"] == pytest.approx(0.2)
        assert outcome.returns["1h"] == pytest.approx(-0.1)

    def test_an_unelapsed_horizon_is_none_not_the_latest_price(self) -> None:
        """Otherwise return_1h on a ten-minute-old token would be a ten-minute
        return wearing an hour's name."""
        outcome = label((0.0, 1.0), (60.0, 1.5), now_offset=120.0)
        assert outcome is not None
        assert outcome.returns["1m"] == pytest.approx(0.5)
        assert outcome.returns["5m"] is None
        assert outcome.returns["1h"] is None

    def test_last_known_value_not_interpolation(self) -> None:
        """An interpolated price is a number nobody observed."""
        # Nothing at exactly 60s; the 45s price is the last one known.
        outcome = label((0.0, 1.0), (45.0, 1.25), (400.0, 2.0))
        assert outcome is not None
        assert outcome.returns["1m"] == pytest.approx(0.25)


class TestExcursions:
    def test_mfe_and_mae_capture_the_path_not_the_endpoint(self) -> None:
        """The whole reason §15 asks for them.

        A trade that ran +80% and came back to flat is a different fact from one
        that never moved, and the final return cannot tell them apart.
        """
        outcome = label((0.0, 1.0), (60.0, 1.18), (120.0, 0.85), (180.0, 1.0))
        assert outcome is not None
        assert outcome.mfe == pytest.approx(0.18)
        assert outcome.mae == pytest.approx(-0.15)
        assert outcome.mfe_at_seconds == 60.0
        assert outcome.mae_at_seconds == 120.0

    def test_a_monotonic_rise_has_no_adverse_excursion_below_zero(self) -> None:
        outcome = label((0.0, 1.0), (60.0, 1.05), (120.0, 1.1))
        assert outcome is not None
        assert outcome.mae == pytest.approx(0.05)
        assert outcome.mfe == pytest.approx(0.1)


class TestRefusesRatherThanGuesses:
    def test_no_anchor_price_means_no_outcome(self) -> None:
        """An outcome with no starting price is not an outcome."""
        assert label((0.0, None), (60.0, 1.2)) is None

    def test_no_forward_data_leaves_everything_none(self) -> None:
        outcome = label((0.0, 1.0))
        assert outcome is not None
        assert outcome.mfe is None
        assert outcome.mae is None
        assert outcome.observations_after == 0

    def test_unpriced_forward_observations_are_skipped(self) -> None:
        outcome = label((0.0, 1.0), (60.0, None), (120.0, 1.35))
        assert outcome is not None
        assert outcome.label is BarrierLabel.UPPER
        assert outcome.observations_after == 1


class TestMultipleConfigurations:
    def test_the_same_series_labels_differently_under_different_barriers(self) -> None:
        """Spec §15 asks for several configurations, because '+30% before -20%?'
        is one question and the dataset should answer more than one."""
        points = ((0.0, 1.0), (60.0, 1.25), (3000.0, 1.0))
        tight = label(*points, config=BarrierConfig("tight", 0.20, 0.10))
        wide = label(*points, config=BarrierConfig("wide", 0.50, 0.40))

        assert tight is not None
        assert wide is not None
        assert tight.label is BarrierLabel.UPPER
        assert wide.label is BarrierLabel.TIMEOUT

    def test_a_shorter_time_barrier_can_time_out_first(self) -> None:
        points = ((0.0, 1.0), (1200.0, 1.4))
        short = label(*points, config=BarrierConfig("short", 0.30, 0.20, 900.0))
        long = label(*points, config=BarrierConfig("long", 0.30, 0.20, 3600.0))

        assert short is not None
        assert long is not None
        assert short.label is BarrierLabel.TIMEOUT
        assert long.label is BarrierLabel.UPPER


def test_outcomes_are_independent_of_features() -> None:
    """The separation that keeps both honest.

    Features come from ``up_to(t)``; outcomes come from what follows ``t``.
    Nothing joins them except a stored observation id, so a pipeline cannot
    accidentally compute both from the same window.
    """
    full = series((0.0, 1.0), (60.0, 1.4), (120.0, 2.0))
    # Everything at or before T0 is invisible to the outcome.
    assert len(full.up_to(T0)) == 1
    outcome = compute_outcome(full, at=T0, config=CONFIG, now=T0 + timedelta(hours=2))
    assert outcome is not None
    assert outcome.observations_after == 2
