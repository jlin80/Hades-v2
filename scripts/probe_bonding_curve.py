"""Evidence for D14 — what the bonding-curve constants actually are.

D14 recorded that two derivations of one token's progress disagreed (33% by SOL
raised, 65% by tokens remaining) and concluded "at least one constant is wrong".
That conclusion was wrong. Both constants are right and both numbers are correct;
they are simply different functions of the same curve state, and the curve is
convex, so they do not agree anywhere except at the endpoints.

This probe demonstrates that from live data and from the arithmetic:

1. A token at t=0 pins the initial reserves exactly.
2. The constant product those imply puts graduation at 85.005 SOL raised, which
   is the figure pump.fun is documented to use.
3. Feeding 65% of tokens-sold through the curve returns 32.6% of SOL raised —
   the reported pair, reproduced, with no wrong constant anywhere.
4. Live tokens are sampled across the whole range to show the two curves apart.

It also reports the two findings that *do* obstruct a naive implementation: pools
that are not SOL-quoted, and pools that do not satisfy the classic constant
product at all.

    .venv/Scripts/python scripts/probe_bonding_curve.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from hades.providers.http import ProviderHttpClient
from hades.providers.pumpfun import BASE_URL, SOURCE

# Measured, not assumed: a token with real_sol_reserves == 0 reports exactly
# these, and every freshly created token in the sample agreed.
INITIAL_VIRTUAL_SOL = 30_000_000_000  # 30 SOL, 9 decimals
INITIAL_VIRTUAL_TOKEN = 1_073_000_000_000_000  # 1.073e9 tokens, 6 decimals
INITIAL_REAL_TOKEN = 793_100_000_000_000  # 793.1e6 tokens sellable from the curve
K = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKEN

# virtual_token - real_token. Held for every non-graduated token sampled, and it
# is the more useful invariant than the initial pair because it does not assume
# a token started where the classic parameters say it did.
CURVE_FLOOR = INITIAL_VIRTUAL_TOKEN - INITIAL_REAL_TOKEN  # 279.9e12

WRAPPED_SOL_QUOTE = "11111111111111111111111111111111"


def sol_raised_at_token_progress(fraction_sold: float) -> float:
    """SOL in the curve once ``fraction_sold`` of the sellable supply is gone."""
    virtual_token = INITIAL_VIRTUAL_TOKEN - fraction_sold * INITIAL_REAL_TOKEN
    return K / virtual_token - INITIAL_VIRTUAL_SOL


def graduation_sol() -> float:
    """SOL raised when the last sellable token leaves the curve."""
    return sol_raised_at_token_progress(1.0)


async def main() -> None:
    client = ProviderHttpClient(SOURCE, base_url=BASE_URL, timeout_seconds=25.0)
    items: list[dict[str, Any]] = []
    try:
        for sort in ("market_cap", "last_trade_timestamp", "created_timestamp"):
            payload = await client.get_json(
                "/coins",
                params={
                    "offset": 0,
                    "limit": 50,
                    "sort": sort,
                    "order": "DESC",
                    "includeNsfw": "true",
                },
            )
            if isinstance(payload, list):
                items.extend(x for x in payload if isinstance(x, dict))
    finally:
        await client.aclose()

    seen: set[str] = set()
    tokens: list[dict[str, Any]] = []
    for item in items:
        mint = item.get("mint")
        if isinstance(mint, str) and mint not in seen:
            seen.add(mint)
            tokens.append(item)

    live = [t for t in tokens if t.get("complete") is False]
    print(f"sampled {len(tokens)} distinct tokens, {len(live)} still on the curve\n")

    print("=== 1. the constants, from a token at t=0 ===")
    fresh = [t for t in live if t.get("real_sol_reserves") == 0]
    if fresh:
        t = fresh[0]
        print(f"  virtual_sol_reserves   {t['virtual_sol_reserves']:>18,}")
        print(f"  virtual_token_reserves {t['virtual_token_reserves']:>18,}")
        print(f"  real_token_reserves    {t['real_token_reserves']:>18,}")
        print(
            f"  matches the assumed initial reserves: {
                t['virtual_sol_reserves'] == INITIAL_VIRTUAL_SOL
                and t['virtual_token_reserves'] == INITIAL_VIRTUAL_TOKEN
                and t['real_token_reserves'] == INITIAL_REAL_TOKEN
            }"
        )
    else:
        print("  no token at t=0 in this sample")

    print("\n=== 2. where those constants put graduation ===")
    print(f"  real_sol at real_token_reserves == 0: {graduation_sol() / 1e9:.4f} SOL")
    print("  (pump.fun's documented graduation threshold is ~85 SOL)")

    print("\n=== 3. the reported 33% vs 65%, reproduced ===")
    sol_at_65 = sol_raised_at_token_progress(0.65)
    print(f"  at 65% of sellable tokens sold, SOL raised is {sol_at_65 / 1e9:.3f} SOL")
    print(f"  as a fraction of graduation:     {sol_at_65 / graduation_sol() * 100:.1f}%")
    print("  Neither constant is wrong. The curve is convex, so the same state is")
    print("  65% through by tokens and 33% through by SOL. They agree only at 0 and 1.")

    print("\n=== 4. the two curves, over live tokens ===")
    print(f"  {'tokens %':>9} {'sol %':>9}")
    rows = []
    for t in live:
        real_token = t.get("real_token_reserves")
        real_sol = t.get("real_sol_reserves")
        if not isinstance(real_token, int) or not isinstance(real_sol, int):
            continue
        rows.append(
            (
                (INITIAL_REAL_TOKEN - real_token) / INITIAL_REAL_TOKEN * 100,
                real_sol / graduation_sol() * 100,
            )
        )
    for by_token, by_sol in sorted(rows, reverse=True)[:12]:
        print(f"  {by_token:9.2f} {by_sol:9.2f}")

    print("\n=== 5. what would break a naive implementation ===")
    off_curve = [
        t
        for t in live
        if isinstance(t.get("virtual_sol_reserves"), int)
        and isinstance(t.get("virtual_token_reserves"), int)
        and abs(t["virtual_sol_reserves"] * t["virtual_token_reserves"] / K - 1) > 1e-9
    ]
    print(f"  tokens not on the classic constant product: {len(off_curve)}/{len(live)}")
    print("  They report virtual_sol well under 30 SOL, so a single hardcoded")
    print("  initial pair is simply wrong for them.")

    non_sol = [t for t in live if t.get("quote_mint") not in (WRAPPED_SOL_QUOTE, None)]
    print(f"\n  pools not quoted in SOL: {len(non_sol)}/{len(live)}")
    for t in non_sol:
        print(
            f"    quote_mint={t.get('quote_mint')} quote_decimals={t.get('quote_decimals')} "
            f"market_cap={t.get('market_cap')} market_cap_quote={t.get('market_cap_quote')}"
        )
    print("  derive_market treats the quote leg as SOL at 9 decimals unconditionally.")

    floors = {
        t["virtual_token_reserves"] - t["real_token_reserves"]
        for t in live
        if isinstance(t.get("virtual_token_reserves"), int)
        and isinstance(t.get("real_token_reserves"), int)
    }
    print(f"\n  virtual_token - real_token over live tokens: {sorted(floors)}")
    print(f"  (constant at {CURVE_FLOOR:,} — the invariant that does hold universally)")


if __name__ == "__main__":
    asyncio.run(main())
