"""Fills computed from the bonding curve.

The point of these is that slippage is *derived*, not assumed. A hardcoded
slippage percentage is a free parameter that quietly decides whether a backtest
shows an edge, so the properties below are what make the number trustworthy.
"""

from __future__ import annotations

import pytest

from hades.paper.curve import estimate_buy_slippage, simulate_buy, simulate_sell

# Real reserves, captured from pump.fun for COTUS on 2026-08-17, in whole units.
VIRTUAL_SOL = 58.027910356
VIRTUAL_TOKENS = 554_733_056.434731
SPOT = VIRTUAL_SOL / VIRTUAL_TOKENS


class TestBuy:
    def test_a_buy_produces_tokens_at_a_worse_price_than_spot(self) -> None:
        fill = simulate_buy(
            sol_in=1.0, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS, fee_rate=0.0
        )
        assert fill is not None
        assert fill.tokens_out > 0
        assert fill.effective_price > fill.spot_price
        assert fill.slippage_fraction > 0

    def test_the_constant_product_is_preserved(self) -> None:
        """The invariant the whole calculation rests on."""
        fill = simulate_buy(
            sol_in=5.0, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS, fee_rate=0.0
        )
        assert fill is not None
        k_before = VIRTUAL_SOL * VIRTUAL_TOKENS
        k_after = (VIRTUAL_SOL + 5.0) * (VIRTUAL_TOKENS - fill.tokens_out)
        assert k_after == pytest.approx(k_before, rel=1e-12)

    def test_slippage_grows_with_order_size(self) -> None:
        """The property that makes this a real constraint on position sizing."""
        sizes = [0.01, 0.1, 1.0, 10.0]
        slippages = [
            estimate_buy_slippage(
                sol_in=size, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS
            )
            for size in sizes
        ]
        assert all(value is not None for value in slippages)
        assert slippages == sorted(slippages)  # type: ignore[type-var]

    def test_a_tiny_order_has_almost_no_slippage(self) -> None:
        slippage = estimate_buy_slippage(
            sol_in=0.001, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS
        )
        assert slippage is not None
        assert slippage < 0.0001

    def test_an_order_comparable_to_the_pool_moves_the_price_a_lot(self) -> None:
        """Why MAX SLIPPAGE is a real gate on a 28 SOL curve, not a formality."""
        slippage = estimate_buy_slippage(
            sol_in=VIRTUAL_SOL, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS
        )
        assert slippage is not None
        assert slippage == pytest.approx(1.0, rel=1e-9)

    def test_the_fee_is_taken_before_the_curve(self) -> None:
        """So it worsens the effective price on top of slippage, as a fee does."""
        free = simulate_buy(
            sol_in=1.0, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS, fee_rate=0.0
        )
        charged = simulate_buy(
            sol_in=1.0, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS, fee_rate=0.01
        )
        assert free is not None
        assert charged is not None
        assert charged.fee_sol == pytest.approx(0.01)
        assert charged.tokens_out < free.tokens_out


class TestSell:
    def test_a_sell_receives_less_than_spot(self) -> None:
        fill = simulate_sell(
            tokens_in=1_000_000.0,
            virtual_sol=VIRTUAL_SOL,
            virtual_tokens=VIRTUAL_TOKENS,
            fee_rate=0.0,
        )
        assert fill is not None
        assert fill.sol_out > 0
        assert fill.effective_price < fill.spot_price
        assert fill.slippage_fraction > 0

    def test_a_round_trip_loses_exactly_the_frictions(self) -> None:
        """The number that decides whether an edge survives.

        Buy then immediately sell the same tokens against the same curve. What
        comes back is strictly less than what went in, and the gap is slippage
        both ways plus the fee twice — the friction V1's realised PnL understated
        until its Stage 2 hardening found the buy-side fee was never captured.
        """
        sol_in = 0.05
        buy = simulate_buy(sol_in=sol_in, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS)
        assert buy is not None

        # Sell back into the curve as the buy left it.
        after_sol = VIRTUAL_SOL + (sol_in - buy.fee_sol)
        after_tokens = VIRTUAL_TOKENS - buy.tokens_out
        sell = simulate_sell(
            tokens_in=buy.tokens_out, virtual_sol=after_sol, virtual_tokens=after_tokens
        )
        assert sell is not None

        assert sell.sol_out < sol_in
        loss = sol_in - sell.sol_out
        # Two 1% fees dominate at this size; slippage on 0.05 SOL against a
        # 58 SOL curve is negligible.
        assert loss == pytest.approx(sol_in * 0.02, rel=0.02)

    def test_selling_into_a_curve_the_buy_moved_is_not_free(self) -> None:
        """A round trip cannot profit from its own price impact."""
        buy = simulate_buy(
            sol_in=10.0, virtual_sol=VIRTUAL_SOL, virtual_tokens=VIRTUAL_TOKENS, fee_rate=0.0
        )
        assert buy is not None
        sell = simulate_sell(
            tokens_in=buy.tokens_out,
            virtual_sol=VIRTUAL_SOL + 10.0,
            virtual_tokens=VIRTUAL_TOKENS - buy.tokens_out,
            fee_rate=0.0,
        )
        assert sell is not None
        assert sell.sol_out == pytest.approx(10.0, rel=1e-9)


class TestRefusesRatherThanGuesses:
    """None, not a zero fill. They are different claims."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"sol_in": 0.0, "virtual_sol": VIRTUAL_SOL, "virtual_tokens": VIRTUAL_TOKENS},
            {"sol_in": -1.0, "virtual_sol": VIRTUAL_SOL, "virtual_tokens": VIRTUAL_TOKENS},
            {"sol_in": 1.0, "virtual_sol": 0.0, "virtual_tokens": VIRTUAL_TOKENS},
            {"sol_in": 1.0, "virtual_sol": VIRTUAL_SOL, "virtual_tokens": 0.0},
            {"sol_in": 1.0, "virtual_sol": -5.0, "virtual_tokens": VIRTUAL_TOKENS},
        ],
    )
    def test_a_buy_with_unusable_inputs_is_none(self, kwargs: dict[str, float]) -> None:
        assert simulate_buy(**kwargs) is None

    def test_a_fee_of_one_hundred_percent_is_refused(self) -> None:
        assert (
            simulate_buy(
                sol_in=1.0,
                virtual_sol=VIRTUAL_SOL,
                virtual_tokens=VIRTUAL_TOKENS,
                fee_rate=1.0,
            )
            is None
        )

    def test_a_sell_with_unusable_inputs_is_none(self) -> None:
        assert simulate_sell(tokens_in=0.0, virtual_sol=1.0, virtual_tokens=1.0) is None
        assert simulate_sell(tokens_in=1.0, virtual_sol=0.0, virtual_tokens=1.0) is None
