"""DexScreener — fallback discovery provider.

Probed live on 2026-08-13: `/token-profiles/latest/v1` returned 30 entries in
0.23s, each carrying `chainId` and `tokenAddress`.

This is deliberately a *lower-fidelity* fallback, not an equivalent second
source. The endpoint is a feed of recently updated token profiles across every
chain, so entries are filtered to Solana and yield an address only — symbol,
name and pool age come back as None.

That is the honest representation. task.md §7 is explicit that NULL beats an
invented value, and `/latest/dex/search` was rejected for this role precisely
because it is a text search returning cross-chain results (the first hit during
probing was a Base pool), not a discovery feed.

The fallback exists so discovery does not stop when the primary is unavailable.
It is not a claim that both sources see the same universe of tokens.
"""

import httpx2

from hades.clock import utc_now
from hades.discovery.errors import ProviderSchemaError
from hades.discovery.http import get_json
from hades.discovery.models import DiscoveredToken
from hades.observability.logging import get_logger

logger = get_logger(__name__)

PROVIDER_NAME = "dexscreener"
TOKEN_PROFILES_ENDPOINT = "/token-profiles/latest/v1"  # noqa: S105 — a URL path, not a secret
SOLANA_CHAIN_ID = "solana"


class DexScreenerProvider:
    """Discovers Solana token addresses from DexScreener's latest profiles."""

    def __init__(self, client: httpx2.AsyncClient, base_url: str, max_retries: int) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def discover_tokens(self) -> list[DiscoveredToken]:
        payload = await get_json(
            self._client,
            provider=PROVIDER_NAME,
            url=f"{self._base_url}{TOKEN_PROFILES_ENDPOINT}",
            endpoint=TOKEN_PROFILES_ENDPOINT,
            max_retries=self._max_retries,
        )
        observed_at = utc_now()

        if not isinstance(payload, list):
            raise ProviderSchemaError(
                provider=PROVIDER_NAME,
                endpoint=TOKEN_PROFILES_ENDPOINT,
                error_type="UnexpectedEnvelope",
                message=f"expected a JSON array, got {type(payload).__name__}",
            )

        tokens: list[DiscoveredToken] = []
        other_chains = 0

        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if entry.get("chainId") != SOLANA_CHAIN_ID:
                other_chains += 1
                continue

            address = entry.get("tokenAddress")
            if not isinstance(address, str) or not address:
                continue

            tokens.append(
                DiscoveredToken(
                    token_address=address,
                    # This endpoint carries no ticker, name or pool creation
                    # time. They stay None rather than being guessed.
                    symbol=None,
                    name=None,
                    pool_address=None,
                    first_seen_at=None,
                    observed_at=observed_at,
                    provider_name=PROVIDER_NAME,
                    raw=entry,
                )
            )

        logger.debug(
            "provider_entries_filtered",
            provider=PROVIDER_NAME,
            endpoint=TOKEN_PROFILES_ENDPOINT,
            received=len(payload),
            solana=len(tokens),
            other_chains=other_chains,
        )
        return tokens
