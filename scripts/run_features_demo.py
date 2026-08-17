"""Compute the feature vector over a live token's real series.

Collects real snapshots for one Pump.fun token, then shows the vector at
several points along its life. Spec §22: run it on real data, not just fixtures
— a feature that works on a hand-made series and produces nonsense on a real one
is not a working feature.

    python scripts/run_features_demo.py [seconds]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from hades.features.engine import compute_features, feature_names
from hades.features.series import Observation, SnapshotSeries
from hades.providers.pumpfun import PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider, collect_new_tokens

INTERVAL_SECONDS = 10.0


async def pick_live_mint() -> str | None:
    """Take a freshly created token straight off the WebSocket."""
    tokens = await collect_new_tokens(PumpPortalProvider(), seconds=30.0, limit=1)
    return tokens[0].token_address if tokens else None


async def main(seconds: float) -> int:
    mint = await pick_live_mint()
    if mint is None:
        print("No token creation seen in 30s; cannot demo on real data.")
        return 1

    provider = PumpFunProvider()
    observations: list[Observation] = []
    print(f"collecting ~{seconds:.0f}s of real snapshots for {mint}\n")

    deadline = asyncio.get_running_loop().time() + seconds
    created_at: datetime | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            snapshot = await provider.fetch_snapshot(mint)
        # Broad on purpose: a demo reports whatever went wrong, it does not hide it.
        except Exception as exc:
            print(f"  snapshot failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(INTERVAL_SECONDS)
            continue

        if created_at is None:
            token = await provider.fetch_token(mint)
            created_at = token.created_at

        age = (
            (snapshot.observed_at - created_at).total_seconds() if created_at is not None else None
        )
        observations.append(
            Observation(
                observed_at=snapshot.observed_at,
                token_age_seconds=age,
                price_sol=snapshot.price_sol,
                market_cap_sol=snapshot.market_cap_sol,
                liquidity_sol=snapshot.liquidity_sol,
                market_cap_usd=snapshot.market_cap_usd,
                real_token_reserves=snapshot.real_token_reserves,
                reply_count=snapshot.reply_count,
                last_trade_at=snapshot.last_trade_at,
                is_complete=snapshot.is_complete,
            )
        )
        print(
            f"  t={age if age is not None else float('nan'):>6.1f}s  "
            f"price={snapshot.price_sol:.4e}  mcap={snapshot.market_cap_sol:.3f} SOL"
            if snapshot.price_sol and snapshot.market_cap_sol
            else "  (incomplete snapshot)"
        )
        await asyncio.sleep(INTERVAL_SECONDS)

    await provider.aclose()

    if len(observations) < 3:
        print("\nToo few observations to compute a meaningful vector.")
        return 1

    series = SnapshotSeries(observations)
    print(f"\n{len(series)} observations, span {series.span_seconds():.1f}s")
    print("=" * 78)

    # The vector at three points, to show it evolving rather than as a snapshot.
    picks = [observations[len(observations) // 3], observations[-2], observations[-1]]
    vectors = [compute_features(series, token_address=mint, as_of=o.observed_at) for o in picks]

    header = "".join(f"{f't={o.token_age_seconds:.0f}s':>16}" for o in picks)
    print(f"{'feature':<36}{header}")
    print("-" * 78)
    for name in feature_names():
        cells = ""
        for vector in vectors:
            value = vector.values[name]
            cells += f"{'-':>16}" if value is None else f"{value:>16.5g}"
        print(f"{name:<36}{cells}")

    missing = [n for n in feature_names() if all(v.values[n] is None for v in vectors)]
    print("\n" + "=" * 78)
    print(f"feature_version {vectors[0].feature_version}, {len(feature_names())} features")
    if missing:
        print(f"never computable on this token ({len(missing)}): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    sys.exit(asyncio.run(main(window)))
