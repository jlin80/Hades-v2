"""Put numbers on the four ways paper trading is optimistic (docs/PAPER_TRADING.md).

The four were listed but never sized, so "the simulation is optimistic" could not
be turned into "discount a result by roughly this much". Two of them are
measurable from live data and are measured here; the other two are bounded from
published figures and stated as assumptions rather than measurements, because
pretending otherwise is the failure this project is organised against.

    .venv/Scripts/python scripts/probe_paper_optimism.py [seconds]

What it measures:

* **Our own curve impact** — exact, from the same reserve math the fills use.
  Not an unmodelled optimism at all; reported so the others have a scale.
* **Other traders' drift** — re-reads live tokens over the modelled latency and
  reports how far the curve moved on its own in that window. This is the part
  the simulator captures only to snapshot resolution.

What it cannot measure here, and why:

* **Priority fees / MEV** — needs submitted transactions to observe, and this
  system has no signer by design (docs/SAFETY.md).
* **Failed transactions** — same; a revert rate is a property of orders we never
  send.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from typing import Any

from hades.paper.curve import estimate_buy_slippage
from hades.providers.http import ProviderHttpClient
from hades.providers.pumpfun import BASE_URL, SOURCE

# The modelled delay between deciding and the order reaching the chain, and the
# position size both defaults come from hades.config.
LATENCY_SECONDS = 2.0
POSITION_SIZE_SOL = 0.02


async def _recent(client: ProviderHttpClient, limit: int) -> list[dict[str, Any]]:
    payload = await client.get_json(
        "/coins",
        params={
            "offset": 0,
            "limit": limit,
            "sort": "last_trade_timestamp",
            "order": "DESC",
            "includeNsfw": "true",
        },
    )
    if not isinstance(payload, list):
        return []
    return [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("complete") is False
        and isinstance(item.get("virtual_sol_reserves"), int)
        and isinstance(item.get("virtual_token_reserves"), int)
    ]


async def main(window_seconds: float) -> None:
    client = ProviderHttpClient(SOURCE, base_url=BASE_URL, timeout_seconds=25.0)
    try:
        first = await _recent(client, 50)
        print("=== 1. our own impact on the curve (modelled exactly) ===")
        print(f"  position size {POSITION_SIZE_SOL} SOL\n")
        print(f"  {'liquidity SOL':>14} {'slippage %':>11}")
        by_liquidity = sorted(first, key=lambda t: t.get("real_sol_reserves") or 0, reverse=True)
        for token in by_liquidity[:5] + by_liquidity[-5:]:
            virtual_sol = token["virtual_sol_reserves"] / 1e9
            virtual_tokens = token["virtual_token_reserves"] / 1e6
            slippage = estimate_buy_slippage(
                sol_in=POSITION_SIZE_SOL,
                virtual_sol=virtual_sol,
                virtual_tokens=virtual_tokens,
            )
            if slippage is None:
                continue
            print(f"  {(token.get('real_sol_reserves') or 0) / 1e9:14.4f} {slippage * 100:11.4f}")

        print(f"\n=== 2. other traders, over the {window_seconds:.0f}s modelled latency ===")
        # Re-fetch the *same* mints rather than the recent list again. That list
        # is sorted by last trade, so re-reading it silently swaps the sample for
        # whichever tokens traded most recently — which both answers a different
        # question and leaves only a handful of survivors to answer it with.
        before = {t["mint"]: t for t in first if isinstance(t.get("mint"), str)}
        await asyncio.sleep(window_seconds)

        moves: list[float] = []
        for mint, previous in before.items():
            token = await client.get_json(f"/coins/{mint}")
            if not isinstance(token, dict):
                continue
            virtual_sol = token.get("virtual_sol_reserves")
            virtual_tokens = token.get("virtual_token_reserves")
            if not isinstance(virtual_sol, int) or not isinstance(virtual_tokens, int):
                continue
            old_price = previous["virtual_sol_reserves"] / previous["virtual_token_reserves"]
            new_price = virtual_sol / virtual_tokens
            if old_price > 0:
                moves.append(abs(new_price - old_price) / old_price)

        if not moves:
            print("  no token was observed twice; rerun with a longer window")
        else:
            moved = [m for m in moves if m > 0]
            print(f"  tokens observed twice:      {len(moves)}")
            print(f"  of those, curve moved:      {len(moved)} ({len(moved) / len(moves):.0%})")
            if moved:
                print(f"  median move when it moved:  {statistics.median(moved) * 100:.4f}%")
                print(f"  worst move observed:        {max(moved) * 100:.4f}%")
            print()
            print("  Compare against column 2 above. Where this exceeds our own modelled")
            print("  slippage, the price other traders move us to matters more than the")
            print("  impact the simulator computes exactly -- and the simulator sees it")
            print("  only at snapshot resolution.")
    finally:
        await client.aclose()

    print("\n=== 3. priority fees / MEV — not measurable here ===")
    print("  Requires submitted transactions. This system has no signer by design.")
    print("  Treat as an unmodelled cost per round trip, not as zero.")

    print("\n=== 4. failed transactions — not measurable here ===")
    print("  Same reason. A revert still pays its fee, so the effect is a haircut on")
    print("  every attempt, applied to the gross count of entries rather than to PnL.")

    print("\n=== 5. drawdown — no longer in this list ===")
    print("  peak_equity_sol was max(start, now), which cannot see a high the account")
    print("  reached and gave back, so drawdown was understated. It is now a true")
    print("  high-water mark over the realised-PnL series. Still realised-only: an")
    print("  open position at a large unrealised profit does not raise the peak.")


if __name__ == "__main__":
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else LATENCY_SECONDS
    asyncio.run(main(seconds))
