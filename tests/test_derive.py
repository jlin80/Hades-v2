"""Deriving market values from curve reserves, pinned to a real observation."""

from __future__ import annotations

import pytest

from hades.tracking.derive import derive_market, derive_sol_price_usd

# Captured from pump.fun frontend-api-v3 for COTUS on 2026-08-17.
COTUS = {
    "virtual_sol_reserves": 58_027_910_356,
    "virtual_token_reserves": 554_733_056_434_731,
    "real_sol_reserves": 28_027_910_356,
    "total_supply": 1_000_000_000_000_000,
    "base_decimals": 6,
    "quote_decimals": 9,
}
# What pump.fun itself reported for the same token, at the same moment.
PROVIDER_MARKET_CAP_SOL = 104.60510633518983
PROVIDER_MARKET_CAP_USD = 7905.9820717484145


def test_derived_market_cap_matches_the_providers_own_figure() -> None:
    """The check that makes these formulas evidence rather than assumption.

    A decimals mistake would be off by a factor of a thousand and could not
    agree to five decimal places by accident.
    """
    derived = derive_market(**COTUS)
    assert derived.market_cap_sol == pytest.approx(PROVIDER_MARKET_CAP_SOL, rel=1e-9)


def test_price_and_liquidity_for_the_same_observation() -> None:
    derived = derive_market(**COTUS)
    assert derived.price_sol == pytest.approx(1.0460510633518983e-7, rel=1e-9)
    # Pre-graduation there is no AMM pool: the SOL in the curve is the liquidity.
    assert derived.liquidity_sol == pytest.approx(28.027910356)


def test_sol_price_is_implied_by_the_two_quoted_caps() -> None:
    implied = derive_sol_price_usd(PROVIDER_MARKET_CAP_SOL, PROVIDER_MARKET_CAP_USD)
    assert implied == pytest.approx(75.58, abs=0.01)


class TestMissingInputsGiveNoneNotZero:
    """A zero price is a claim about the market. A missing one is the truth."""

    def test_no_reserves_means_no_price(self) -> None:
        derived = derive_market(
            virtual_sol_reserves=None,
            virtual_token_reserves=None,
            real_sol_reserves=None,
            total_supply=None,
        )
        assert derived.price_sol is None
        assert derived.market_cap_sol is None
        assert derived.liquidity_sol is None

    def test_zero_token_reserves_does_not_divide_by_zero(self) -> None:
        derived = derive_market(
            virtual_sol_reserves=1,
            virtual_token_reserves=0,
            real_sol_reserves=None,
            total_supply=None,
        )
        assert derived.price_sol is None

    def test_negative_reserves_do_not_produce_a_negative_price(self) -> None:
        """A negative reserve is a corrupt payload, not a curve state."""
        derived = derive_market(
            virtual_sol_reserves=1_000_000_000,
            virtual_token_reserves=-1,
            real_sol_reserves=None,
            total_supply=None,
        )
        assert derived.price_sol is None

    def test_market_cap_needs_supply(self) -> None:
        derived = derive_market(
            virtual_sol_reserves=58_027_910_356,
            virtual_token_reserves=554_733_056_434_731,
            real_sol_reserves=None,
            total_supply=None,
        )
        assert derived.price_sol is not None
        assert derived.market_cap_sol is None

    def test_sol_price_needs_a_non_zero_cap(self) -> None:
        assert derive_sol_price_usd(0.0, 100.0) is None
        assert derive_sol_price_usd(None, 100.0) is None
        assert derive_sol_price_usd(100.0, None) is None


def test_provider_decimals_override_the_defaults() -> None:
    """The defaults are pump.fun's current values, not a law.

    Reading them per token means a program variant with different decimals
    produces a different price rather than a wrong one.
    """
    default = derive_market(
        virtual_sol_reserves=1_000_000_000,
        virtual_token_reserves=1_000_000,
        real_sol_reserves=None,
        total_supply=None,
    )
    explicit = derive_market(
        virtual_sol_reserves=1_000_000_000,
        virtual_token_reserves=1_000_000,
        real_sol_reserves=None,
        total_supply=None,
        base_decimals=9,
        quote_decimals=9,
    )
    assert default.price_sol != explicit.price_sol
