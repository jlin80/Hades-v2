"""EARLY MOMENTUM — the one hypothesis (spec §12).

## ⚠️ This is a hypothesis, not a claim

Nothing here asserts these conditions are profitable. Spec §12 is explicit that
the hypothesis must be configurable and must not be presented as truth, and
every threshold below is a **starting point chosen to be plausible, not a value
derived from evidence.** There is no evidence yet. Producing it is what Phase 7
is for, and the answer may be that this hypothesis has no edge at all.

## What §12 asks for, and what is actually computable

Spec §12 sketches:

    Token young + buying activity accelerating + volume increasing
    + sell pressure below threshold + liquidity above minimum

Three of those five need per-trade data that no free source supplies —
`buyer_velocity`, volume, and the gross buy/sell split that "sell pressure"
requires. See `docs/DATA_SOURCES.md` and `docs/FEATURES.md`.

**The substitution is stated rather than made quietly:**


* *buying activity accelerating* -> `market_cap_acceleration` > 0.
  Lost: this is acceleration of price, not of buyer count.
* *volume increasing* -> `liquidity_velocity` > 0.
  Lost: it is a **net** flow; gross buys and sells are inseparable from it.
* *sell pressure below threshold* -> net flow being positive at all.
  Lost: selling that is merely outweighed by buying is invisible.
* *token young* -> `token_age_seconds` in range. Nothing lost.
* *liquidity above minimum* -> `liquidity_sol` >= min. Nothing lost.

The third row is the weakest link and worth naming plainly: a token with heavy
buying *and* heavy selling looks identical to one with light buying and no
selling, if their net flows match. That is a real blind spot in this hypothesis,
not a rounding error, and closing it costs a funded PumpPortal key.

Two conditions are added that §12 does not ask for, because the data made them
necessary:

* **an activity gate** — `price_movement_ratio`, because a token whose curve has
  not moved has a velocity of exactly zero for reasons that are not momentum;
* **data-quality gates** — enough observations to trust a second derivative, and
  a recent enough reading to be about now.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hades.signals.models import Condition, MarketState, Signal

NAME = "early_momentum"
# Bump when a condition is added, removed or redefined. A stored signal's
# meaning is fixed by the version that produced it.
VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class EarlyMomentumConfig:
    """Every threshold, all configurable, none of them evidence-based yet.

    The window suffix is part of the configuration because the choice of
    lookback *is* part of the hypothesis: "accelerating over 30 seconds" and
    "accelerating over 5 minutes" are different claims about the world.
    """

    window: str = "30s"

    # "Token young". The lower bound is not zero: a token needs a few
    # observations before any velocity means anything, and the tracker's
    # measured ~12s interval means under ~25s there is nothing to differentiate.
    min_token_age_seconds: float = 30.0
    max_token_age_seconds: float = 300.0

    # Momentum. Market cap in SOL per second — at the ~30 SOL caps seen on live
    # tokens, 0.05 SOL/s is roughly a 0.15%/s climb.
    min_market_cap_velocity: float = 0.05
    # Accelerating, i.e. climbing faster than it was. Zero is the natural
    # boundary and the only threshold here that is not arbitrary.
    min_market_cap_acceleration: float = 0.0

    # Net SOL entering the curve. Strictly positive: the closest available
    # stand-in for "buy pressure exceeds sell pressure".
    min_liquidity_velocity: float = 0.0

    # Activity gate. At the measured ~12s interval a 30s window holds ~3
    # observations, so 0.5 means the curve moved in at least half of them.
    min_price_movement_ratio: float = 0.5
    # Inactivity filter: a token that has not traded recently is not in a
    # momentum regime whatever its trailing velocity says.
    max_seconds_since_last_trade: float = 30.0

    # Spec §13's MIN LIQUIDITY, applied here as well as in the risk engine.
    # V1's measurement is the reason this is not higher: raising it to $25k
    # equivalent left ~13 candidates a day out of ~2,100.
    min_liquidity_sol: float = 1.0

    # Data quality. Three observations is the floor for an acceleration, so
    # below it the acceleration condition is unevaluable rather than false.
    min_observations: float = 3.0
    max_freshness_seconds: float = 30.0


class EarlyMomentumStrategy:
    """Emits a research signal when every condition holds.

    All-or-nothing rather than a weighted score, deliberately. A score needs
    weights, weights need evidence, and there is none — V1's DynamicWeightEngine
    combined regime, Sharpe, profit factor, drawdown, consistency, sample size,
    AI and research into a single number before any component had demonstrated
    an edge. Conjunction is the honest form for an untested hypothesis: it is
    interpretable, and every clause is recorded so the binding one is visible.
    """

    def __init__(self, config: EarlyMomentumConfig | None = None) -> None:
        self.config = config or EarlyMomentumConfig()
        self.name = NAME
        self.version = VERSION

    async def evaluate(self, market_state: MarketState) -> Signal | None:
        conditions = self.check(market_state)
        if not all(condition.passed for condition in conditions):
            return None
        return Signal(
            token_address=market_state.token_address,
            strategy=self.name,
            strategy_version=self.version,
            created_at=market_state.as_of,
            conditions=conditions,
        )

    def check(self, market_state: MarketState) -> tuple[Condition, ...]:
        """Evaluate every clause, passing or failing, and return them all.

        Always evaluates all of them rather than short-circuiting: the point of
        recording a rejection is to know which clause was binding, and a
        short-circuit would only ever name the first.
        """
        config = self.config
        window = config.window

        def feature(name: str) -> float | None:
            return market_state.feature(name)

        return (
            _between(
                "token_age",
                feature("token_age_seconds"),
                config.min_token_age_seconds,
                config.max_token_age_seconds,
                "Token is inside the early window the hypothesis is about.",
            ),
            _at_least(
                "market_cap_velocity",
                feature(f"market_cap_velocity_{window}"),
                config.min_market_cap_velocity,
                "Market cap is climbing. Stands in for §12's 'volume increasing'.",
            ),
            _at_least(
                "market_cap_acceleration",
                feature(f"market_cap_acceleration_{window}"),
                config.min_market_cap_acceleration,
                "Climbing faster than it was. Stands in for 'buying activity "
                "accelerating' -- it is price acceleration, not buyer acceleration.",
            ),
            _at_least(
                "liquidity_velocity",
                feature(f"liquidity_velocity_{window}"),
                config.min_liquidity_velocity,
                "Net SOL entering the curve. The closest available stand-in for "
                "'sell pressure below threshold', and it cannot see selling that "
                "is merely outweighed by buying.",
            ),
            _at_least(
                "price_movement_ratio",
                feature(f"price_movement_ratio_{window}"),
                config.min_price_movement_ratio,
                "The curve actually moved. Not in §12; added because a dormant "
                "token has a velocity of exactly zero for reasons that are not "
                "momentum.",
            ),
            _at_most(
                "seconds_since_last_trade",
                feature("seconds_since_last_trade"),
                config.max_seconds_since_last_trade,
                "Traded recently. A stale token is not in a momentum regime "
                "whatever its trailing velocity says.",
            ),
            _at_least(
                "liquidity_sol",
                feature("liquidity_sol"),
                config.min_liquidity_sol,
                "Spec §13's MIN LIQUIDITY, and §12's 'liquidity above minimum'.",
            ),
            _at_least(
                "observations",
                feature(f"observation_count_{window}"),
                config.min_observations,
                "Enough observations for a second derivative to mean anything.",
            ),
            _at_most(
                "freshness",
                feature("freshness_seconds"),
                config.max_freshness_seconds,
                "The reading is about now. Spec §13: never decide on stale data.",
            ),
        )

    def with_config(self, **overrides: float | str) -> EarlyMomentumStrategy:
        """A copy with adjusted thresholds. For sweeps, not for production drift."""
        return EarlyMomentumStrategy(replace(self.config, **overrides))  # type: ignore[arg-type]


def _at_least(name: str, value: float | None, threshold: float, description: str) -> Condition:
    """Fails when the value is missing.

    Unknown is not permission. A condition whose input is None means we could
    not evaluate the hypothesis, and firing on that would be firing blind.
    """
    return Condition(
        name=name,
        passed=value is not None and value >= threshold,
        value=value,
        threshold=threshold,
        description=description,
    )


def _at_most(name: str, value: float | None, threshold: float, description: str) -> Condition:
    return Condition(
        name=name,
        passed=value is not None and value <= threshold,
        value=value,
        threshold=threshold,
        description=description,
    )


def _between(
    name: str, value: float | None, lower: float, upper: float, description: str
) -> Condition:
    return Condition(
        name=name,
        passed=value is not None and lower <= value <= upper,
        value=value,
        threshold=upper,
        description=description,
    )
