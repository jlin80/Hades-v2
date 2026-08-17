"""Every feature, and the leakage guarantee.

Spec §10: each feature must have tests. The most important test in the file is
``test_future_observations_cannot_change_a_vector`` — everything else here would
fail noisily if it regressed, and that one would fail looking like success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hades.features.definitions import (
    curve_consumption_velocity,
    drawdown_from_max,
    freshness_seconds,
    is_graduated,
    liquidity_acceleration,
    liquidity_return,
    liquidity_velocity,
    market_cap_return,
    market_cap_velocity,
    max_market_cap_sol,
    max_sampling_gap_seconds,
    mean_sampling_interval_seconds,
    observation_count,
    price_acceleration,
    price_movement_ratio,
    price_return,
    price_velocity,
    reply_count,
    reply_velocity,
    seconds_since_last_trade,
    series_span_seconds,
    token_age_seconds,
)
from hades.features.engine import (
    FEATURE_VERSION,
    FeatureWindows,
    compute_features,
    feature_names,
)
from hades.features.series import Observation, SnapshotSeries

T0 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"


def observation(
    offset: float,
    *,
    price: float | None = None,
    market_cap: float | None = None,
    liquidity: float | None = None,
    replies: int | None = None,
    real_tokens: int | None = None,
    last_trade_offset: float | None = None,
    complete: bool | None = None,
) -> Observation:
    at = T0 + timedelta(seconds=offset)
    return Observation(
        observed_at=at,
        token_age_seconds=offset,
        price_sol=price,
        market_cap_sol=market_cap,
        liquidity_sol=liquidity,
        reply_count=replies,
        real_token_reserves=real_tokens,
        last_trade_at=(
            at - timedelta(seconds=last_trade_offset) if last_trade_offset is not None else None
        ),
        is_complete=complete,
    )


def series(*observations: Observation) -> SnapshotSeries:
    return SnapshotSeries(list(observations))


# A clean 10-second series where price doubles over 20s.
RISING = series(
    observation(0, price=1.0, market_cap=100.0, liquidity=10.0, replies=0, real_tokens=1000),
    observation(10, price=1.5, market_cap=150.0, liquidity=15.0, replies=2, real_tokens=900),
    observation(20, price=2.0, market_cap=200.0, liquidity=22.0, replies=5, real_tokens=760),
)


class TestNoLookAhead:
    def test_future_observations_cannot_change_a_vector(self) -> None:
        """The one that would fail looking like success.

        Every other regression here shows up as a wrong number. Temporal leakage
        shows up as *better* offline metrics from a model that cannot work — it
        is the failure Hades V1 designed its whole knowledge loop to make
        impossible to write, and this is the equivalent guarantee here.
        """
        past = series(
            observation(0, price=1.0, market_cap=100.0),
            observation(10, price=1.5, market_cap=150.0),
        )
        with_future = series(
            observation(0, price=1.0, market_cap=100.0),
            observation(10, price=1.5, market_cap=150.0),
            # A violent move after the decision point.
            observation(20, price=99.0, market_cap=9900.0),
            observation(30, price=0.01, market_cap=1.0),
        )
        at = T0 + timedelta(seconds=10)

        without = compute_features(past, token_address=MINT, as_of=at)
        including = compute_features(with_future, token_address=MINT, as_of=at)

        assert without.values == including.values

    def test_up_to_is_inclusive_of_the_boundary(self) -> None:
        """An observation exactly at the decision instant is information we had."""
        full = series(observation(0, price=1.0), observation(10, price=2.0))
        assert len(full.up_to(T0 + timedelta(seconds=10))) == 2
        assert len(full.up_to(T0 + timedelta(seconds=9.999))) == 1

    def test_drawdown_never_sees_a_future_peak(self) -> None:
        with_future_peak = series(
            observation(0, market_cap=100.0),
            observation(10, market_cap=90.0),
            observation(20, market_cap=1000.0),
        )
        visible = with_future_peak.up_to(T0 + timedelta(seconds=10))
        # 10% off a peak of 100, not 91% off a peak of 1000.
        assert drawdown_from_max(visible) == pytest.approx(0.1)


class TestPointFeatures:
    def test_token_age(self) -> None:
        assert token_age_seconds(RISING) == 20.0

    def test_latest_values_come_from_the_last_observation(self) -> None:
        assert max_market_cap_sol(RISING) == 200.0
        assert reply_count(RISING) == 5.0

    def test_seconds_since_last_trade(self) -> None:
        quiet = series(observation(0, price=1.0, last_trade_offset=45.0))
        assert seconds_since_last_trade(quiet) == 45.0

    def test_seconds_since_last_trade_is_none_without_a_trade_time(self) -> None:
        assert seconds_since_last_trade(series(observation(0, price=1.0))) is None

    def test_graduation_flag(self) -> None:
        assert is_graduated(series(observation(0, complete=True))) == 1.0
        assert is_graduated(series(observation(0, complete=False))) == 0.0
        assert is_graduated(series(observation(0))) is None


class TestRates:
    def test_price_velocity_uses_real_elapsed_time(self) -> None:
        """Not the configured interval. Measured, they differ by ~20%."""
        uneven = series(observation(0, price=1.0), observation(40, price=3.0))
        assert price_velocity(uneven) == pytest.approx(0.05)

    def test_market_cap_velocity(self) -> None:
        assert market_cap_velocity(RISING) == pytest.approx(5.0)

    def test_liquidity_velocity_is_signed(self) -> None:
        """Positive is net SOL entering the curve; negative is net leaving."""
        draining = series(observation(0, liquidity=20.0), observation(10, liquidity=15.0))
        assert liquidity_velocity(draining) == pytest.approx(-0.5)

    def test_reply_velocity(self) -> None:
        assert reply_velocity(RISING) == pytest.approx(0.25)

    def test_curve_consumption_is_positive_when_tokens_are_bought(self) -> None:
        """Sign is inverted from the raw reserve on purpose.

        The reserve falls as the token is bought; a feature that goes down when
        activity goes up would eventually be read backwards.
        """
        assert curve_consumption_velocity(RISING) == pytest.approx(12.0)

    def test_a_single_observation_has_no_rate(self) -> None:
        alone = series(observation(0, price=1.0, market_cap=100.0))
        assert price_velocity(alone) is None
        assert market_cap_velocity(alone) is None

    def test_zero_elapsed_time_does_not_divide_by_zero(self) -> None:
        simultaneous = series(observation(0, price=1.0), observation(0, price=2.0))
        assert price_velocity(simultaneous) is None


class TestReturns:
    def test_price_return(self) -> None:
        assert price_return(RISING) == pytest.approx(1.0)

    def test_market_cap_return(self) -> None:
        assert market_cap_return(RISING) == pytest.approx(1.0)

    def test_liquidity_return(self) -> None:
        assert liquidity_return(RISING) == pytest.approx(1.2)

    def test_a_zero_start_gives_none_not_infinity(self) -> None:
        """Infinity in a feature propagates into every statistic built on it."""
        from_zero = series(observation(0, price=0.0), observation(10, price=1.0))
        assert price_return(from_zero) is None


class TestAcceleration:
    def test_accelerating_price_is_positive(self) -> None:
        accelerating = series(
            observation(0, price=1.0),
            observation(10, price=1.1),
            observation(20, price=1.5),
            observation(30, price=2.5),
        )
        value = price_acceleration(accelerating)
        assert value is not None
        assert value > 0

    def test_decelerating_price_is_negative(self) -> None:
        decelerating = series(
            observation(0, price=1.0),
            observation(10, price=2.0),
            observation(20, price=2.4),
            observation(30, price=2.5),
        )
        value = price_acceleration(decelerating)
        assert value is not None
        assert value < 0

    def test_constant_velocity_has_zero_acceleration(self) -> None:
        linear = series(
            observation(0, price=1.0),
            observation(10, price=2.0),
            observation(20, price=3.0),
            observation(30, price=4.0),
        )
        assert price_acceleration(linear) == pytest.approx(0.0, abs=1e-12)

    def test_two_observations_are_not_enough(self) -> None:
        """A velocity needs two points; a change in velocity needs two velocities."""
        two = series(observation(0, price=1.0), observation(10, price=2.0))
        assert price_acceleration(two) is None

    def test_three_observations_are_enough(self) -> None:
        """The minimum, and it must actually work at the minimum.

        Found by running on a live token: an earlier version split the series at
        its midpoint *in time*, and real timestamps are uneven enough that the
        midpoint falls between observations — leaving one half holding a single
        point. Every 30-second acceleration came back None despite the window
        containing three observations.
        """
        exactly_three = series(
            observation(0, price=1.0),
            observation(10, price=1.5),
            observation(20, price=3.0),
        )
        assert price_acceleration(exactly_three) is not None

    def test_uneven_real_world_timestamps_still_compute(self) -> None:
        """The exact shape that failed: three points, no round midpoint."""
        uneven = series(
            observation(23.412, price=3.594e-8),
            observation(33.531, price=3.510e-8),
            observation(43.615, price=2.931e-8),
        )
        value = price_acceleration(uneven)
        assert value is not None
        # Falling faster in the second half than in the first, so negative.
        assert value < 0

    def test_liquidity_acceleration_is_the_closest_thing_to_buyer_acceleration(self) -> None:
        """§10 asks for buyer_acceleration, which needs per-trade data we lack.

        Inflow accelerating is what is computable, and it does not claim to know
        how many buyers produced it.
        """
        surging = series(
            observation(0, liquidity=10.0),
            observation(10, liquidity=10.5),
            observation(20, liquidity=12.0),
            observation(30, liquidity=16.0),
        )
        value = liquidity_acceleration(surging)
        assert value is not None
        assert value > 0


class TestPathFeatures:
    def test_drawdown_is_zero_at_the_peak(self) -> None:
        assert drawdown_from_max(RISING) == pytest.approx(0.0)

    def test_drawdown_after_a_fall(self) -> None:
        fell = series(
            observation(0, market_cap=100.0),
            observation(10, market_cap=200.0),
            observation(20, market_cap=150.0),
        )
        assert drawdown_from_max(fell) == pytest.approx(0.25)

    def test_price_movement_ratio_counts_intervals_that_moved(self) -> None:
        """The honest substitute for transaction_velocity.

        It measures whether trades happened per interval, not how many, and it
        saturates at 1.0 precisely when the token is busiest.
        """
        partly_quiet = series(
            observation(0, price=1.0),
            observation(10, price=1.0),
            observation(20, price=1.2),
            observation(30, price=1.2),
            observation(40, price=1.5),
        )
        assert price_movement_ratio(partly_quiet) == pytest.approx(0.5)

    def test_a_completely_quiet_token_scores_zero(self) -> None:
        quiet = series(*(observation(i * 10, price=1.0) for i in range(5)))
        assert price_movement_ratio(quiet) == 0.0

    def test_a_constantly_trading_token_saturates(self) -> None:
        busy = series(*(observation(i * 10, price=1.0 + i) for i in range(5)))
        assert price_movement_ratio(busy) == 1.0


class TestDataQualityFeatures:
    def test_counts_and_span(self) -> None:
        assert observation_count(RISING) == 3.0
        assert series_span_seconds(RISING) == 20.0

    def test_mean_interval_is_measured_not_assumed(self) -> None:
        uneven = series(observation(0, price=1.0), observation(12), observation(25))
        assert mean_sampling_interval_seconds(uneven) == pytest.approx(12.5)

    def test_max_gap_exposes_a_hole_in_the_series(self) -> None:
        """A velocity spanning a hole is an average over time we did not watch."""
        gapped = series(observation(0), observation(10), observation(300), observation(310))
        assert max_sampling_gap_seconds(gapped) == 290.0

    def test_freshness_is_our_own_data_age(self) -> None:
        """Not the provider's record age, which measures trade inactivity."""
        stale = series(observation(0, price=1.0))
        assert freshness_seconds(stale, T0 + timedelta(seconds=90)) == 90.0

    def test_an_empty_series_has_no_span(self) -> None:
        assert series_span_seconds(series()) is None
        assert observation_count(series()) == 0.0


class TestEngine:
    def test_vector_carries_everything_needed_to_reproduce_it(self) -> None:
        vector = compute_features(RISING, token_address=MINT, as_of=T0 + timedelta(seconds=20))
        assert vector.token_address == MINT
        assert vector.feature_version == FEATURE_VERSION
        assert vector.observed_at == T0 + timedelta(seconds=20)

    def test_every_declared_name_is_present(self) -> None:
        """A vector missing a column silently becomes a NULL in the dataset."""
        vector = compute_features(RISING, token_address=MINT, as_of=T0 + timedelta(seconds=20))
        assert set(vector.values) == set(feature_names())

    def test_names_are_stable_across_calls(self) -> None:
        """A dataset whose columns reorder cannot have its exports concatenated."""
        assert feature_names() == feature_names()

    def test_windows_produce_suffixed_features(self) -> None:
        vector = compute_features(RISING, token_address=MINT, as_of=T0 + timedelta(seconds=20))
        assert "price_velocity_30s" in vector.values
        assert "price_velocity_60s" in vector.values

    def test_a_short_window_sees_fewer_observations(self) -> None:
        vector = compute_features(RISING, token_address=MINT, as_of=T0 + timedelta(seconds=20))
        # 30s back from t=20 covers all three; a 5s window covers only the last.
        assert vector.values["observation_count_30s"] == 3.0

        narrow = compute_features(
            RISING,
            token_address=MINT,
            as_of=T0 + timedelta(seconds=20),
            windows=FeatureWindows(seconds=(5.0,)),
        )
        assert narrow.values["observation_count_5s"] == 1.0
        # One observation cannot support a velocity, and says so.
        assert narrow.values["price_velocity_5s"] is None

    def test_an_empty_series_produces_a_full_vector_of_nones(self) -> None:
        """Never a partial vector: a missing key and a null are different bugs."""
        vector = compute_features(series(), token_address=MINT, as_of=T0)
        assert set(vector.values) == set(feature_names())
        assert vector.values["price_sol"] is None
        assert vector.values["observation_count"] == 0.0

    def test_missing_inputs_give_none_not_zero(self) -> None:
        """A zero velocity claims the price did not move. That is a measurement."""
        no_prices = series(observation(0), observation(10))
        vector = compute_features(no_prices, token_address=MINT, as_of=T0 + timedelta(seconds=10))
        assert vector.values["price_velocity_30s"] is None
        assert vector.values["price_sol"] is None

    def test_deterministic(self) -> None:
        at = T0 + timedelta(seconds=20)
        first = compute_features(RISING, token_address=MINT, as_of=at)
        second = compute_features(RISING, token_address=MINT, as_of=at)
        assert first.values == second.values

    def test_unsorted_input_is_sorted_before_use(self) -> None:
        """Order comes from observed_at, not from however the rows arrived."""
        shuffled = series(
            observation(20, price=2.0),
            observation(0, price=1.0),
            observation(10, price=1.5),
        )
        assert price_return(shuffled) == pytest.approx(1.0)
