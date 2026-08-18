"""The one hypothesis, clause by clause.

Every threshold here is a plausible starting point rather than a value derived
from evidence, so these tests pin *behaviour*, not correctness of the thresholds
themselves. Whether the hypothesis has an edge is unmeasured — Phase 7's job.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hades.features.engine import feature_names
from hades.signals.early_momentum import EarlyMomentumConfig, EarlyMomentumStrategy
from hades.signals.models import MarketState
from hades.signals.strategy import Strategy

T0 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"

# A state that satisfies every clause. Each test breaks exactly one.
PASSING: dict[str, float | None] = {
    "token_age_seconds": 60.0,
    "market_cap_velocity_30s": 0.5,
    "market_cap_acceleration_30s": 0.01,
    "liquidity_velocity_30s": 0.2,
    "price_movement_ratio_30s": 1.0,
    "seconds_since_last_trade": 5.0,
    "liquidity_sol": 12.0,
    "observation_count_30s": 4.0,
    "freshness_seconds": 2.0,
}


def state(**overrides: float | None) -> MarketState:
    """A full feature vector, so `feature()` never raises on a missing key."""
    features: dict[str, float | None] = dict.fromkeys(feature_names())
    features.update(PASSING)
    features.update(overrides)
    return MarketState(token_address=MINT, as_of=T0, feature_version="1.0.0", features=features)


@pytest.fixture
def strategy() -> EarlyMomentumStrategy:
    return EarlyMomentumStrategy()


class TestProtocol:
    def test_the_strategy_satisfies_the_spec_interface(
        self, strategy: EarlyMomentumStrategy
    ) -> None:
        """Spec §12 asks for name + evaluate, and nothing more."""
        assert isinstance(strategy, Strategy)
        assert strategy.name == "early_momentum"
        assert strategy.version


class TestFiring:
    async def test_fires_when_every_clause_holds(self, strategy: EarlyMomentumStrategy) -> None:
        signal = await strategy.evaluate(state())
        assert signal is not None
        assert signal.token_address == MINT
        assert signal.strategy == "early_momentum"
        assert not signal.failed

    async def test_the_signal_is_timestamped_from_the_observation(
        self, strategy: EarlyMomentumStrategy
    ) -> None:
        """Not wall-clock now.

        Spec §13 needs signal_created_at comparable with the data it came from;
        stamping the current time would fold our processing delay into its age.
        """
        signal = await strategy.evaluate(state())
        assert signal is not None
        assert signal.created_at == T0

    async def test_conjunction_not_a_score(self, strategy: EarlyMomentumStrategy) -> None:
        """One failed clause is enough.

        A weighted score would need weights, and weights need evidence there is
        none of. V1 combined eight factors into one number before any component
        had demonstrated an edge.
        """
        assert await strategy.evaluate(state(liquidity_sol=0.1)) is None


class TestEachClauseCanBlock:
    @pytest.mark.parametrize(
        ("override", "clause"),
        [
            ({"token_age_seconds": 5.0}, "token_age"),
            ({"token_age_seconds": 9999.0}, "token_age"),
            ({"market_cap_velocity_30s": 0.0}, "market_cap_velocity"),
            ({"market_cap_acceleration_30s": -0.5}, "market_cap_acceleration"),
            ({"liquidity_velocity_30s": -0.5}, "liquidity_velocity"),
            ({"price_movement_ratio_30s": 0.0}, "price_movement_ratio"),
            ({"seconds_since_last_trade": 600.0}, "seconds_since_last_trade"),
            ({"liquidity_sol": 0.01}, "liquidity_sol"),
            ({"observation_count_30s": 2.0}, "observations"),
            ({"freshness_seconds": 300.0}, "freshness"),
        ],
    )
    async def test_a_broken_clause_blocks_the_signal(
        self, strategy: EarlyMomentumStrategy, override: dict[str, float], clause: str
    ) -> None:
        assert await strategy.evaluate(state(**override)) is None

        failed = {c.name for c in strategy.check(state(**override)) if not c.passed}
        assert failed == {clause}


class TestMissingDataNeverFires:
    @pytest.mark.parametrize("missing", sorted(PASSING))
    async def test_a_none_feature_blocks_the_signal(
        self, strategy: EarlyMomentumStrategy, missing: str
    ) -> None:
        """Unknown is not permission.

        A None input means the hypothesis could not be evaluated. Firing on that
        would be firing blind, and it is exactly the case that arises most often
        early in a token's life, when the vector is thinnest.
        """
        assert await strategy.evaluate(state(**{missing: None})) is None


class TestExplainability:
    def test_every_clause_is_recorded_pass_or_fail(self, strategy: EarlyMomentumStrategy) -> None:
        """§17 asks how results vary with age, liquidity and activity.

        Answering that needs to know which clause was binding, so a rejection
        records all of them rather than the first that failed.
        """
        conditions = strategy.check(state(liquidity_sol=0.01, freshness_seconds=999.0))
        assert len(conditions) == 9
        assert {c.name for c in conditions if not c.passed} == {"liquidity_sol", "freshness"}

    def test_conditions_carry_value_and_threshold(self, strategy: EarlyMomentumStrategy) -> None:
        by_name = {c.name: c for c in strategy.check(state(liquidity_sol=0.01))}
        blocked = by_name["liquidity_sol"]
        assert blocked.value == 0.01
        assert blocked.threshold == 1.0
        assert blocked.description

    def test_no_clause_short_circuits(self, strategy: EarlyMomentumStrategy) -> None:
        """All nine are evaluated even when the first already failed."""
        conditions = strategy.check(state(token_age_seconds=1.0, liquidity_sol=0.01))
        assert len(conditions) == 9


class TestConfigurability:
    """Spec §12: the hypothesis must be configurable, not presented as truth."""

    async def test_thresholds_change_the_outcome(self) -> None:
        blocked = state(market_cap_velocity_30s=0.01)
        assert await EarlyMomentumStrategy().evaluate(blocked) is None

        permissive = EarlyMomentumStrategy(EarlyMomentumConfig(min_market_cap_velocity=0.0))
        assert await permissive.evaluate(blocked) is not None

    async def test_the_window_is_part_of_the_hypothesis(self) -> None:
        """'Accelerating over 30s' and 'over 60s' are different claims."""
        sixty = EarlyMomentumStrategy(EarlyMomentumConfig(window="60s"))
        features = dict(PASSING)
        # Passing on 30s, failing on 60s.
        features["market_cap_velocity_60s"] = 0.0
        for name in ("market_cap_acceleration", "liquidity_velocity", "price_movement_ratio"):
            features[f"{name}_60s"] = PASSING[f"{name}_30s"]
        features["observation_count_60s"] = 4.0

        assert await sixty.evaluate(state(**features)) is None

    def test_with_config_does_not_mutate_the_original(self) -> None:
        base = EarlyMomentumStrategy()
        swept = base.with_config(min_liquidity_sol=99.0)
        assert base.config.min_liquidity_sol == 1.0
        assert swept.config.min_liquidity_sol == 99.0


class TestUnknownFeatureIsAnError:
    def test_a_typo_raises_rather_than_silently_never_firing(self) -> None:
        """The failure mode this prevents is a hypothesis that looks inert.

        A misspelled feature returning None would make its clause fail forever,
        and the strategy would look like one that never triggers rather than
        like a bug.
        """
        market_state = state()
        with pytest.raises(KeyError, match="unknown feature"):
            market_state.feature("markt_cap_velocity_30s")
