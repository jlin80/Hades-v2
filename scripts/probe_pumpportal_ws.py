"""Phase 1 evidence: PumpPortal's WebSocket as a push-based discovery path.

Answers two questions, and records the answer to the second one because it
constrains what Phase 4 can compute:

1. Does ``subscribeNewToken`` deliver real, verifiable token creations, and at
   what rate? (It does. Measured ~0.24/s.)
2. Does ``subscribeTokenTrade`` give us a per-trade stream? (It does not, for
   free — see the gate message it returns.)

    python scripts/probe_pumpportal_ws.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

WS_URL = "wss://pumpportal.fun/api/data"
PUMP_API = "https://frontend-api-v3.pump.fun"
USER_AGENT = "hades-v2-phase1-probe/0.1 (data source evaluation)"
CAPTURE_SECONDS = 45


async def capture_new_tokens() -> list[dict[str, object]]:
    """Listen to subscribeNewToken and return the frames received."""
    frames: list[dict[str, object]] = []
    async with websockets.connect(WS_URL, open_timeout=15) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        deadline = time.monotonic() + CAPTURE_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            try:
                frames.append(json.loads(raw))
            except json.JSONDecodeError:
                print(f"  non-JSON frame: {str(raw)[:120]}")
    return frames


async def check_trade_stream(mints: list[str]) -> str:
    """Try to subscribe to per-trade events. Returns whatever the server says."""
    async with websockets.connect(WS_URL, open_timeout=15) as ws:
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": mints}))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
        except TimeoutError:
            return "<no response in 20s>"
    return str(raw)[:400]


async def confirm_against_pumpfun(mint: str) -> str:
    """Cross-check a pushed mint against the primary source."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        response = await client.get(f"{PUMP_API}/coins/{mint}")
        if response.status_code != 200:
            return f"HTTP {response.status_code}"
        created = response.json().get("created_timestamp")
        return f"confirmed, created_timestamp={created}"


async def main() -> int:
    print(f"subscribeNewToken — capturing {CAPTURE_SECONDS}s")
    frames = await capture_new_tokens()
    tokens = [f for f in frames if isinstance(f.get("mint"), str)]
    rate = len(tokens) / CAPTURE_SECONDS
    print(f"  {len(frames)} frames, {len(tokens)} carrying a mint")
    print(f"  creation rate {rate:.2f}/s  (~{rate * 86400:,.0f}/day)")

    if not tokens:
        print("  no token events captured — cannot continue")
        return 1

    print("\n  field set of a creation event:")
    for key, value in sorted(tokens[0].items()):
        print(f"    {key:<24} = {json.dumps(value)[:100]}")

    mint = str(tokens[0]["mint"])
    confirmation = await confirm_against_pumpfun(mint)
    print(f"\n  cross-check {mint[:16]}... against pump.fun: {confirmation}")

    print("\nsubscribeTokenTrade — is a per-trade stream available?")
    print(f"  server said: {await check_trade_stream([str(t['mint']) for t in tokens[:6]])}")
    print(
        "\n  A per-trade stream is what would make unique_buyers, unique_sellers,\n"
        "  buy_volume and sell_volume computable exactly. See docs/DATA_SOURCES.md."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
