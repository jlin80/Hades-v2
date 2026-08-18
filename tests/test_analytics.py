"""§17's questions, and the ways a summary can quietly mislead."""

from __future__ import annotations

import pytest

from hades.outcomes.labels import BarrierLabel
from hades.research.analytics import LabelledRecord, bucket_by, summarise


def record(
    label: BarrierLabel,
    *,
    mfe: float | None = 0.1,
    mae: float | None = -0.05,
    age: float | None = 60.0,
    liquidity: float | None = 10.0,
    returns: dict[str, float | None] | None = None,
    had_signal: bool = True,
) -> LabelledRecord:
    return LabelledRecord(
        token_address="mint",
        features={"token_age_seconds": age, "liquidity_sol": liquidity},
        label=label,
        mfe=mfe,
        mae=mae,
        returns=returns or {"1h": 0.0},
        had_signal=had_signal,
    )


class TestCounts:
    def test_it_answers_how_many_of_each(self) -> None:
        summary = summarise(
            [
                record(BarrierLabel.UPPER),
                record(BarrierLabel.UPPER),
                record(BarrierLabel.LOWER),
                record(BarrierLabel.TIMEOUT),
            ]
        )
        assert summary.total == 4
        assert (summary.upper, summary.lower, summary.timeout) == (2, 1, 1)
        assert summary.upper_rate == pytest.approx(0.5)

    def test_an_empty_population_reports_none_not_zero(self) -> None:
        """A rate over zero records is undefined, not 0%."""
        summary = summarise([])
        assert summary.total == 0
        assert summary.upper_rate is None
        assert summary.expectancy is None
        assert summary.mean_mfe is None


class TestUnresolvedIsExcludedAndCounted:
    def test_unresolved_records_do_not_enter_any_rate(self) -> None:
        """Mixing them in biases every statistic toward whatever the newest
        tokens happened to be doing -- the exact population under study."""
        summary = summarise(
            [
                record(BarrierLabel.UPPER),
                record(BarrierLabel.LOWER),
                record(BarrierLabel.UNRESOLVED),
                record(BarrierLabel.UNRESOLVED),
            ]
        )
        assert summary.total == 4
        assert summary.resolved == 2
        assert summary.unresolved == 2
        assert summary.upper_rate == pytest.approx(0.5)

    def test_a_wholly_unresolved_population_has_no_rates(self) -> None:
        summary = summarise([record(BarrierLabel.UNRESOLVED)])
        assert summary.resolved == 0
        assert summary.upper_rate is None


class TestExpectancyAndProfitFactor:
    def test_expectancy_is_in_the_units_of_the_labelling_scheme(self) -> None:
        summary = summarise(
            [record(BarrierLabel.UPPER), record(BarrierLabel.LOWER)],
            upper_fraction=0.30,
            lower_fraction=0.20,
        )
        assert summary.expectancy == pytest.approx(0.05)

    def test_different_barriers_give_different_expectancy(self) -> None:
        """Which is why the fractions must be passed in.

        Reporting one expectancy without naming the scheme it came from would
        be meaningless.
        """
        records = [record(BarrierLabel.UPPER), record(BarrierLabel.LOWER)]
        tight = summarise(records, upper_fraction=0.20, lower_fraction=0.10)
        wide = summarise(records, upper_fraction=0.50, lower_fraction=0.25)
        assert tight.expectancy != wide.expectancy

    def test_a_timeout_counts_at_what_it_was_actually_worth(self) -> None:
        """Not zero.

        Treating every timeout as break-even hides a strategy that times out
        consistently underwater.
        """
        summary = summarise([record(BarrierLabel.TIMEOUT, returns={"1h": -0.12})])
        assert summary.expectancy == pytest.approx(-0.12)

    def test_profit_factor(self) -> None:
        summary = summarise(
            [record(BarrierLabel.UPPER), record(BarrierLabel.UPPER), record(BarrierLabel.LOWER)],
            upper_fraction=0.30,
            lower_fraction=0.20,
        )
        assert summary.profit_factor == pytest.approx(0.6 / 0.2)

    def test_no_losses_gives_none_not_infinity(self) -> None:
        """A strategy that has never lost has not been measured, it has been
        lucky so far."""
        summary = summarise([record(BarrierLabel.UPPER)])
        assert summary.profit_factor is None


class TestExcursions:
    def test_mean_and_median_mfe_and_mae(self) -> None:
        summary = summarise(
            [
                record(BarrierLabel.UPPER, mfe=0.5, mae=-0.1),
                record(BarrierLabel.LOWER, mfe=0.1, mae=-0.3),
            ]
        )
        assert summary.mean_mfe == pytest.approx(0.3)
        assert summary.mean_mae == pytest.approx(-0.2)
        assert summary.median_mfe == pytest.approx(0.3)

    def test_missing_excursions_are_skipped_not_zeroed(self) -> None:
        summary = summarise(
            [record(BarrierLabel.UPPER, mfe=0.4), record(BarrierLabel.UPPER, mfe=None)]
        )
        assert summary.mean_mfe == pytest.approx(0.4)


class TestBucketing:
    def test_results_can_be_split_by_a_feature(self) -> None:
        """The §17 question that matters most.

        An aggregate cannot say whether a hypothesis works everywhere or only in
        one corner -- and 'only in one corner' is the far more common truth.
        """
        records = [
            record(BarrierLabel.UPPER, age=10.0),
            record(BarrierLabel.UPPER, age=20.0),
            record(BarrierLabel.LOWER, age=200.0),
            record(BarrierLabel.LOWER, age=250.0),
        ]
        buckets = bucket_by(records, "token_age_seconds", [100.0])

        assert len(buckets) == 2
        assert buckets[0].summary.upper_rate == pytest.approx(1.0)
        assert buckets[1].summary.upper_rate == pytest.approx(0.0)

    def test_bucket_names_describe_their_edges(self) -> None:
        buckets = bucket_by([], "liquidity_sol", [5.0, 25.0])
        assert [b.name for b in buckets] == [
            "liquidity_sol < 5",
            "5 <= liquidity_sol < 25",
            "liquidity_sol >= 25",
        ]

    def test_records_missing_the_feature_join_no_bucket(self) -> None:
        """An unmeasured token is not a small one."""
        records = [
            record(BarrierLabel.UPPER, liquidity=1.0),
            record(BarrierLabel.LOWER, liquidity=None),
        ]
        buckets = bucket_by(records, "liquidity_sol", [10.0])
        assert sum(b.summary.total for b in buckets) == 1

    def test_bucketing_preserves_the_barrier_units(self) -> None:
        buckets = bucket_by(
            [record(BarrierLabel.UPPER, age=10.0)],
            "token_age_seconds",
            [100.0],
            upper_fraction=0.5,
            lower_fraction=0.25,
        )
        assert buckets[0].summary.expectancy == pytest.approx(0.5)


def test_signal_count_is_separate_from_the_population() -> None:
    """Outcomes are recorded for every observation, signal or not (§15).

    The observations without a signal are the counterfactual: without them there
    is nothing to compare the strategy's picks against.
    """
    summary = summarise(
        [
            record(BarrierLabel.UPPER, had_signal=True),
            record(BarrierLabel.LOWER, had_signal=False),
            record(BarrierLabel.TIMEOUT, had_signal=False),
        ]
    )
    assert summary.total == 3
    assert summary.with_signal == 1
