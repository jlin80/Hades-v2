"""Provider response fixtures.

These are trimmed copies of responses actually captured from the live endpoints
on 2026-08-13 via scripts/probe-providers.sh — not invented shapes. A parser
tested against an imagined response proves nothing about the real one.
"""

from typing import Any

# GeckoTerminal /networks/solana/new_pools, one pool entry.
GECKOTERMINAL_POOL: dict[str, Any] = {
    "id": "solana_3tDxSsENwvF4tXS81KZjVJnCNf1GhBAaQhnqLxWbJJ8T",
    "type": "pool",
    "attributes": {
        "base_token_price_usd": "0.00000213574158753335194253884364279333651237052820112385",
        "address": "3tDxSsENwvF4tXS81KZjVJnCNf1GhBAaQhnqLxWbJJ8T",
        "name": "TRENCHTOK / SOL",
        "pool_created_at": "2026-08-13T20:22:58Z",
        "fdv_usd": "2135.741588",
        "market_cap_usd": None,
        "price_change_percentage": {"m5": "0", "m15": "0", "m30": "0", "h1": "0"},
        "transactions": {
            "m5": {"buys": 1, "sells": 0, "buyers": 1, "sellers": 0},
            "m15": {"buys": 1, "sells": 0, "buyers": 1, "sellers": 0},
        },
    },
    "relationships": {
        "base_token": {
            "data": {"id": "solana_9BsHRRVeCkKhLcTStBnUcHBmqssJgcbEcphgcopump", "type": "token"}
        },
        "quote_token": {
            "data": {"id": "solana_So11111111111111111111111111111111111111112", "type": "token"}
        },
        "dex": {"data": {"id": "raydium", "type": "dex"}},
    },
}

GECKOTERMINAL_RESPONSE: dict[str, Any] = {"data": [GECKOTERMINAL_POOL]}

# DexScreener /token-profiles/latest/v1, two entries: one Solana, one not.
DEXSCREENER_SOLANA_ENTRY: dict[str, Any] = {
    "url": "https://dexscreener.com/solana/ewamhqxtzwshdkzscengdu15sm6zyurdd2mrzhlbpump",
    "chainId": "solana",
    "tokenAddress": "EwAmHqXTzWsHdKZSCengDu15SM6ZyurDd2mrZhLBpump",
    "icon": "https://cdn.dexscreener.com/cms/images/37wTPcuwlNCd4Eia",
    "description": "a token",
}

DEXSCREENER_OTHER_CHAIN_ENTRY: dict[str, Any] = {
    "url": "https://dexscreener.com/base/0x1131db5977242a03ebead1acd18f80a9a29e5922",
    "chainId": "base",
    "tokenAddress": "0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82",
}

DEXSCREENER_RESPONSE: list[dict[str, Any]] = [
    DEXSCREENER_SOLANA_ENTRY,
    DEXSCREENER_OTHER_CHAIN_ENTRY,
]
