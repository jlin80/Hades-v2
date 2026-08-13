"""GeckoTerminal — primary discovery provider.

Chosen after probing candidates live on 2026-08-13 (`scripts/probe-providers.sh`).
`/networks/solana/new_pools` returned 20 pools in 0.50s with pool creation
times, prices, FDV, price changes across m5/m15/m30/h1/h6/h24, and transaction
counts broken into buys/sells/buyers/sellers — the richest single call of any
candidate, free and without an API key.

The same probe found Pump.fun's `frontend-api` returning Cloudflare 530 and both
Jupiter token endpoints dead (404, and a host that no longer resolves). Those
were load-bearing in v1.

Phase 1 uses only the identity fields. The market data in this response is
deliberately left unparsed until Phase 2 gives it a table to live in; the whole
payload is retained in ``raw`` so nothing is lost in the meantime.
"""

from datetime import datetime
from typing import Any

import httpx2

from hades.clock import utc_now
from hades.discovery.errors import ProviderSchemaError
from hades.discovery.http import get_json
from hades.discovery.models import DiscoveredToken
from hades.observability.logging import get_logger

logger = get_logger(__name__)

PROVIDER_NAME = "geckoterminal"
NEW_POOLS_ENDPOINT = "/networks/solana/new_pools"

# GeckoTerminal identifies tokens as "solana_<address>" in relationship links.
_ID_PREFIX = "solana_"


class GeckoTerminalProvider:
    """Discovers tokens from GeckoTerminal's new Solana pools feed."""

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
            url=f"{self._base_url}{NEW_POOLS_ENDPOINT}",
            endpoint=NEW_POOLS_ENDPOINT,
            max_retries=self._max_retries,
            params={"page": 1},
        )
        observed_at = utc_now()

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderSchemaError(
                provider=PROVIDER_NAME,
                endpoint=NEW_POOLS_ENDPOINT,
                error_type="UnexpectedEnvelope",
                message=f"expected object with a 'data' array, got {type(payload).__name__}",
            )

        tokens: list[DiscoveredToken] = []
        skipped = 0

        for pool in payload["data"]:
            token = self._parse_pool(pool, observed_at)
            if token is None:
                skipped += 1
                continue
            tokens.append(token)

        if skipped:
            # A pool we cannot parse is not fatal — the feed mixes pool shapes —
            # but a sudden rise here means the schema moved under us.
            logger.warning(
                "provider_pools_skipped",
                provider=PROVIDER_NAME,
                endpoint=NEW_POOLS_ENDPOINT,
                skipped=skipped,
                received=len(payload["data"]),
            )

        return tokens

    def _parse_pool(self, pool: Any, observed_at: datetime) -> DiscoveredToken | None:
        """Extract the base token from one pool entry, or None if unparseable."""
        if not isinstance(pool, dict):
            return None

        attributes = pool.get("attributes")
        relationships = pool.get("relationships")
        if not isinstance(attributes, dict) or not isinstance(relationships, dict):
            return None

        address = _base_token_address(relationships)
        if address is None:
            return None

        return DiscoveredToken(
            token_address=address,
            symbol=_symbol_from_pool_name(attributes.get("name")),
            name=None,  # the feed carries the pool name, not the token name
            pool_address=_as_optional_str(attributes.get("address")),
            first_seen_at=_parse_timestamp(attributes.get("pool_created_at")),
            observed_at=observed_at,
            provider_name=PROVIDER_NAME,
            raw=pool,
        )


def _base_token_address(relationships: dict[str, Any]) -> str | None:
    base = relationships.get("base_token")
    if not isinstance(base, dict):
        return None
    data = base.get("data")
    if not isinstance(data, dict):
        return None
    identifier = data.get("id")
    if not isinstance(identifier, str) or not identifier.startswith(_ID_PREFIX):
        return None
    address = identifier[len(_ID_PREFIX) :]
    return address or None


def _symbol_from_pool_name(pool_name: Any) -> str | None:
    """Derive the base token's symbol from a pool name like ``TRENCHTOK / SOL``.

    Returns None rather than a guess when the format does not match. A wrong
    symbol is worse than a missing one: it is indistinguishable from a real one
    later.
    """
    if not isinstance(pool_name, str) or "/" not in pool_name:
        return None
    symbol = pool_name.split("/", 1)[0].strip()
    return symbol or None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning None if absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
