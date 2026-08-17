"""Deriving market values from raw bonding-curve reserves.

Pure functions, so every number in the dataset can be recomputed from the
stored raw fields and checked. Spec §11 requires the features behind a decision
to stay intact and reproducible; storing a provider's derived price instead
would make that impossible, because there would be nothing to recompute *from*.

## Verification

The formulas below were checked against the provider's own reported values on a
live token (COTUS, 2026-08-17):

    virtual_sol_reserves   58,027,910,356      (9 decimals -> 58.0279 SOL)
    virtual_token_reserves 554,733,056,434,731 (6 decimals -> 554,733,056 tokens)
    total_supply           1,000,000,000,000,000

    price      = 58.0279 / 554,733,056       = 1.0461e-7 SOL/token
    market cap = 1.0461e-7 * 1,000,000,000   = 104.605 SOL

pump.fun reported `market_cap` = 104.60510633518983. Agreement to five decimals
is what makes this a check rather than a guess — a decimals mistake would be off
by a factor of a thousand and could not pass.

`test_derive.py` pins that same case, so a future edit that breaks the formula
fails against a real observation rather than against our own assumption.
"""

from __future__ import annotations

from dataclasses import dataclass

# pump.fun's own fields, when it supplies them, override these.
DEFAULT_BASE_DECIMALS = 6
DEFAULT_QUOTE_DECIMALS = 9


@dataclass(frozen=True, slots=True)
class DerivedMarket:
    """Everything computable from the curve, or None where it is not."""

    price_sol: float | None
    market_cap_sol: float | None
    liquidity_sol: float | None


def derive_market(
    *,
    virtual_sol_reserves: int | None,
    virtual_token_reserves: int | None,
    real_sol_reserves: int | None,
    total_supply: int | None,
    base_decimals: int | None = None,
    quote_decimals: int | None = None,
) -> DerivedMarket:
    """Spot price, market cap and liquidity from the curve's reserves.

    Returns None for anything an input is missing for, rather than substituting
    a zero. A zero price is a claim; a missing one is the truth.
    """
    base = base_decimals if base_decimals is not None else DEFAULT_BASE_DECIMALS
    quote = quote_decimals if quote_decimals is not None else DEFAULT_QUOTE_DECIMALS

    price_sol: float | None = None
    if virtual_sol_reserves is not None and virtual_token_reserves:
        sol = virtual_sol_reserves / (10**quote)
        tokens = virtual_token_reserves / (10**base)
        # `virtual_token_reserves` truthiness above already excludes 0; this
        # guards a negative, which would be a corrupt payload rather than a
        # curve state, and must not silently produce a negative price.
        price_sol = sol / tokens if tokens > 0 else None

    market_cap_sol: float | None = None
    if price_sol is not None and total_supply is not None:
        market_cap_sol = price_sol * (total_supply / (10**base))

    liquidity_sol: float | None = None
    if real_sol_reserves is not None:
        # For a pre-graduation token there is no AMM pool: the SOL actually in
        # the curve *is* the liquidity, and it is the correct definition here.
        liquidity_sol = real_sol_reserves / (10**quote)

    return DerivedMarket(
        price_sol=price_sol,
        market_cap_sol=market_cap_sol,
        liquidity_sol=liquidity_sol,
    )


def derive_sol_price_usd(
    market_cap_sol: float | None, market_cap_usd: float | None
) -> float | None:
    """Implied SOL/USD, from the provider quoting the same cap in both units.

    Free, and worth keeping: it is what lets a SOL-denominated dataset be
    re-expressed in USD later, at the rate that applied *at observation time*
    rather than at analysis time.
    """
    if not market_cap_sol or market_cap_usd is None:
        return None
    return market_cap_usd / market_cap_sol
