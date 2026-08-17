"""Phase 1 evidence collector.

Hits candidate data sources for real and reports what they actually do: status
codes, measured latency, which of the metrics we need are present in the
response, and how they behave under a short burst.

Run it, read the output, and write conclusions into ``docs/DATA_SOURCES.md``.
It exists so that document cites measurements instead of assumptions — Hades V1
shipped adapters for `jupiter` and `meteora` that returned 404 in production.

    python scripts/probe_data_sources.py

Deliberately modest: 5 latency samples per endpoint and a 20-request burst.
Enough to characterise a source, not enough to be abusive to a free service.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

USER_AGENT = "hades-v2-phase1-probe/0.1 (data source evaluation)"
LATENCY_SAMPLES = 5
BURST_SIZE = 20
TIMEOUT = 10.0

# The metrics section 9 of the spec asks for. A source is only useful to the
# extent it supplies these, so presence is checked rather than assumed.
WANTED_METRICS = (
    "price",
    "market_cap",
    "liquidity",
    "volume",
    "buy_count",
    "sell_count",
    "buy_volume",
    "sell_volume",
    "unique_buyers",
    "unique_sellers",
    "bonding_curve_progress",
    "holder_count",
    "created_at",
)


@dataclass
class ProbeResult:
    name: str
    url: str
    reachable: bool = False
    status: int | None = None
    latencies_ms: list[float] = field(default_factory=list)
    metrics_found: dict[str, str] = field(default_factory=dict)
    burst_ok: int = 0
    burst_429: int = 0
    burst_other: int = 0
    note: str = ""

    @property
    def p50(self) -> float | None:
        return round(statistics.median(self.latencies_ms), 1) if self.latencies_ms else None

    @property
    def worst(self) -> float | None:
        return round(max(self.latencies_ms), 1) if self.latencies_ms else None


def _walk(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Flatten a JSON document to (dotted_path, leaf_value) pairs."""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out += _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        # Only the first element: shapes repeat, and we want a field map.
        if node:
            out += _walk(node[0], f"{path}[0]")
    else:
        out.append((path, node))
    return out


# Substrings that identify each wanted metric in a provider's own naming. The
# mapping is intentionally loose — providers disagree wildly on names, and a
# false positive here is visible in the report while a false negative hides a
# usable source.
METRIC_HINTS: dict[str, tuple[str, ...]] = {
    "price": ("price_usd", "priceusd", "price_native", "usd_price"),
    "market_cap": ("market_cap", "marketcap", "fdv", "usd_market_cap"),
    "liquidity": ("liquidity", "reserve_in_usd"),
    "volume": ("volume",),
    "buy_count": ("buys", "buy_count", "txns.h1.buys"),
    "sell_count": ("sells", "sell_count"),
    "buy_volume": ("buy_volume", "volume.buys", "buys_volume"),
    "sell_volume": ("sell_volume", "volume.sells", "sells_volume"),
    "unique_buyers": ("buyers", "unique_buyers"),
    "unique_sellers": ("sellers", "unique_sellers"),
    "bonding_curve_progress": (
        "bonding_curve",
        "curve_progress",
        "complete",
        "virtual_sol_reserves",
        "virtual_token_reserves",
    ),
    "holder_count": ("holder", "holders"),
    "created_at": ("created", "pool_created_at", "created_timestamp"),
}


def find_metrics(payload: Any) -> dict[str, str]:
    """Map each wanted metric to the provider field that appears to supply it."""
    flat = _walk(payload)
    found: dict[str, str] = {}
    for metric, hints in METRIC_HINTS.items():
        for dotted, value in flat:
            lowered = dotted.lower()
            if any(hint in lowered for hint in hints) and value is not None:
                found[metric] = dotted
                break
    return found


async def probe(client: httpx.AsyncClient, name: str, url: str, note: str = "") -> ProbeResult:
    result = ProbeResult(name=name, url=url, note=note)

    for _ in range(LATENCY_SAMPLES):
        started = time.perf_counter()
        try:
            response = await client.get(url)
        except (httpx.HTTPError, OSError) as exc:
            result.note = (result.note + f" | transport: {type(exc).__name__}").strip(" |")
            return result
        result.latencies_ms.append((time.perf_counter() - started) * 1000)
        result.status = response.status_code
        if response.status_code == 200 and not result.metrics_found:
            try:
                result.metrics_found = find_metrics(response.json())
            except json.JSONDecodeError:
                result.note = (result.note + " | 200 but body is not JSON").strip(" |")
        await asyncio.sleep(0.2)

    result.reachable = result.status == 200
    return result


async def burst(client: httpx.AsyncClient, result: ProbeResult) -> None:
    """Fire BURST_SIZE concurrent requests to see how the source pushes back."""
    if not result.reachable:
        return

    async def one() -> int | None:
        try:
            return (await client.get(result.url)).status_code
        except (httpx.HTTPError, OSError):
            return None

    for status in await asyncio.gather(*(one() for _ in range(BURST_SIZE))):
        if status == 200:
            result.burst_ok += 1
        elif status == 429:
            result.burst_429 += 1
        else:
            result.burst_other += 1


async def find_live_pumpfun_mint(client: httpx.AsyncClient) -> tuple[str | None, str]:
    """Discover a real, currently-live Pump.fun mint to probe with.

    Hardcoding a mint would make this script rot: meme coins die. Asking a
    source for a fresh one is also, incidentally, the first real test.
    """
    url = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1"
    try:
        response = await client.get(url)
        response.raise_for_status()
        for pool in response.json().get("data", []):
            attrs = pool.get("attributes", {})
            relationships = pool.get("relationships", {})
            dex = relationships.get("dex", {}).get("data", {}).get("id", "")
            if "pump" not in dex.lower():
                continue
            token_id = relationships.get("base_token", {}).get("data", {}).get("id", "")
            mint = token_id.removeprefix("solana_")
            if mint:
                return mint, f"{attrs.get('name', '?')} on dex={dex}"
    except (httpx.HTTPError, OSError, KeyError, ValueError) as exc:
        return None, f"discovery failed: {type(exc).__name__}"
    return None, "no pump.fun pool in the newest page of Solana pools"


def render(results: list[ProbeResult]) -> None:
    print("\n" + "=" * 100)
    print(f"{'source':<34} {'status':>6} {'p50':>8} {'worst':>8}  {'burst ok/429/err':>17}")
    print("=" * 100)
    for r in results:
        p50 = f"{r.p50:.0f}ms" if r.p50 is not None else "-"
        worst = f"{r.worst:.0f}ms" if r.worst is not None else "-"
        status = str(r.status) if r.status is not None else "ERR"
        bursts = f"{r.burst_ok}/{r.burst_429}/{r.burst_other}"
        print(f"{r.name:<34} {status:>6} {p50:>8} {worst:>8}  {bursts:>17}")
        if r.note:
            print(f"{'':<34} note: {r.note}")
        if r.metrics_found:
            missing = [m for m in WANTED_METRICS if m not in r.metrics_found]
            print(f"{'':<34} has ({len(r.metrics_found)}/{len(WANTED_METRICS)}):")
            for metric, path in r.metrics_found.items():
                print(f"{'':<38} {metric:<24} <- {path}")
            if missing:
                print(f"{'':<38} MISSING: {', '.join(missing)}")
        print()


async def main() -> int:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        headers=headers,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=25, max_keepalive_connections=10),
    ) as client:
        mint, how = await find_live_pumpfun_mint(client)
        print(f"probe mint: {mint or '<none>'}  ({how})")
        if mint is None:
            print("Cannot run per-token probes without a live mint. Aborting.")
            return 1

        targets: list[tuple[str, str, str]] = [
            (
                "geckoterminal new_pools",
                "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1",
                "discovery candidate",
            ),
            (
                "geckoterminal token pools",
                f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools",
                "snapshot candidate",
            ),
            (
                "dexscreener token-pairs/v1",
                f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}",
                "current documented path",
            ),
            (
                "dexscreener latest/dex/tokens",
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                "legacy path, still advertised",
            ),
            (
                "pumpfun frontend-api-v3 coin",
                f"https://frontend-api-v3.pump.fun/coins/{mint}",
                "undocumented; expect Cloudflare",
            ),
            (
                "pumpfun frontend-api-v3 latest",
                "https://frontend-api-v3.pump.fun/coins?offset=0&limit=10&sort=created_timestamp",
                "undocumented; expect Cloudflare",
            ),
            (
                "solana public RPC getHealth",
                "https://api.mainnet-beta.solana.com/health",
                "baseline reachability only",
            ),
        ]

        results = [await probe(client, name, url, note) for name, url, note in targets]
        for result in results:
            await burst(client, result)

    render(results)
    usable = [r for r in results if r.reachable and len(r.metrics_found) >= 4]
    print(f"{len(usable)} of {len(results)} endpoints returned 200 with >=4 wanted metrics.")
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
