"""Fills computed from the bonding curve, not estimated.

Spec §14 asks paper execution to model slippage rather than assume perfect
fills. On an AMM that usually means picking a slippage percentage and hoping.
Here it does not have to: a Pump.fun token trades against a constant-product
bonding curve whose reserves we already store, so the fill price for a given
order size is **exactly computable**.

    x * y = k        with x = virtual SOL, y = virtual tokens

Buying with ``sol_in``::

    x' = x + sol_in
    y' = k / x'
    tokens_out = y - y'

and the effective price is ``sol_in / tokens_out``, which is strictly worse than
the spot price ``x / y`` — that difference *is* the slippage, derived rather
than guessed.

This matters more than it might look. A hardcoded slippage assumption is a free
parameter that quietly determines whether a backtest shows an edge; V1's
execution engine had a Slippage Manager and never established that its numbers
matched reality. Here the number comes from the same reserves the features come
from, so a research result can be reproduced from stored data.

## What this does *not* model

* **Other traders.** The curve state is from our last snapshot; between then and
  a real fill, other trades would move it. That gap is why the simulator fills
  against a *later* snapshot (see ``hades.paper.service``), which captures the
  drift but not the ordering within a block.
* **Priority fees and MEV.** A real buy competes for block space. Not modelled,
  and it makes these fills optimistic.
* **Failed transactions.** A real order can revert and still cost a fee.

None of those make the simulation useless; all of them make it *optimistic*,
which is the direction to be honest about when a result looks good.
"""

from __future__ import annotations

from dataclasses import dataclass

# pump.fun's trading fee, as a fraction. Configurable at the call site because
# it is a protocol parameter that has changed before and will change again.
DEFAULT_FEE_RATE = 0.01


@dataclass(frozen=True, slots=True)
class BuyFill:
    """The result of simulating a buy against the curve."""

    sol_in: float
    """Gross SOL committed, before the fee is taken."""

    fee_sol: float
    """Protocol fee, deducted from ``sol_in`` before it reaches the curve."""

    tokens_out: float
    """Tokens received, from the curve, after the fee."""

    spot_price: float
    """Price before the order, ``x / y``."""

    effective_price: float
    """What we actually paid per token, ``(sol_in - fee) / tokens_out``."""

    slippage_fraction: float
    """``(effective - spot) / spot``. Always >= 0 for a buy."""


@dataclass(frozen=True, slots=True)
class SellFill:
    """The result of simulating a sell against the curve."""

    tokens_in: float
    sol_out: float
    """Net SOL received, after the fee."""

    fee_sol: float
    spot_price: float
    effective_price: float
    slippage_fraction: float
    """``(spot - effective) / spot``. Always >= 0 for a sell."""


def _spot_price(virtual_sol: float, virtual_tokens: float) -> float | None:
    if virtual_sol <= 0 or virtual_tokens <= 0:
        return None
    return virtual_sol / virtual_tokens


def simulate_buy(
    *,
    sol_in: float,
    virtual_sol: float,
    virtual_tokens: float,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> BuyFill | None:
    """Simulate buying with ``sol_in`` SOL. All quantities in whole units.

    Returns None when the inputs cannot describe a curve — an empty reserve, a
    non-positive order. None rather than a zero-token fill: a fill that bought
    nothing is a different claim from an order that could not be simulated.

    The fee is taken **before** the curve, matching how a protocol fee works:
    only the remainder buys tokens, so the fee makes the effective price worse
    on top of the slippage rather than being a separate deduction afterwards.
    """
    if sol_in <= 0 or virtual_sol <= 0 or virtual_tokens <= 0 or not 0 <= fee_rate < 1:
        return None

    spot = _spot_price(virtual_sol, virtual_tokens)
    if spot is None:
        return None

    fee = sol_in * fee_rate
    net_in = sol_in - fee
    if net_in <= 0:
        return None

    k = virtual_sol * virtual_tokens
    new_virtual_sol = virtual_sol + net_in
    new_virtual_tokens = k / new_virtual_sol
    tokens_out = virtual_tokens - new_virtual_tokens
    if tokens_out <= 0:
        return None

    effective = net_in / tokens_out
    return BuyFill(
        sol_in=sol_in,
        fee_sol=fee,
        tokens_out=tokens_out,
        spot_price=spot,
        effective_price=effective,
        slippage_fraction=(effective - spot) / spot,
    )


def simulate_sell(
    *,
    tokens_in: float,
    virtual_sol: float,
    virtual_tokens: float,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> SellFill | None:
    """Simulate selling ``tokens_in`` tokens back into the curve.

    The fee is taken **after** the curve here, mirroring a buy: SOL comes out,
    then the protocol takes its cut. Both directions therefore cost the fee, and
    a round trip pays it twice — which is exactly the friction V1's realised PnL
    was missing until its Stage 2 hardening found the buy-side fee was never
    being captured.
    """
    if tokens_in <= 0 or virtual_sol <= 0 or virtual_tokens <= 0 or not 0 <= fee_rate < 1:
        return None

    spot = _spot_price(virtual_sol, virtual_tokens)
    if spot is None:
        return None

    k = virtual_sol * virtual_tokens
    new_virtual_tokens = virtual_tokens + tokens_in
    new_virtual_sol = k / new_virtual_tokens
    gross_out = virtual_sol - new_virtual_sol
    if gross_out <= 0:
        return None

    fee = gross_out * fee_rate
    net_out = gross_out - fee
    if net_out <= 0:
        return None

    effective = net_out / tokens_in
    return SellFill(
        tokens_in=tokens_in,
        sol_out=net_out,
        fee_sol=fee,
        spot_price=spot,
        effective_price=effective,
        slippage_fraction=(spot - effective) / spot,
    )


def estimate_buy_slippage(
    *, sol_in: float, virtual_sol: float, virtual_tokens: float
) -> float | None:
    """Slippage a buy of this size would incur, ignoring fees.

    Used by the risk engine's MAX SLIPPAGE gate (spec §13) *before* committing
    to an order. Fee-free on purpose: the fee is a known constant and folding it
    in would make the gate a check on two different things at once.
    """
    fill = simulate_buy(
        sol_in=sol_in, virtual_sol=virtual_sol, virtual_tokens=virtual_tokens, fee_rate=0.0
    )
    return None if fill is None else fill.slippage_fraction
