"""PRIMARY provider — pump.fun ``frontend-api-v3``.

Chosen in Phase 1: the only source measured to have data from t=0, and the only
one exposing bonding-curve reserves. Undocumented and Cloudflare-fronted, which
is the largest technical risk in the project (``docs/DATA_SOURCES.md``).

Phase 2 uses two of its capabilities:

* ``list_recent`` — polling discovery, and the backfill for anything the
  WebSocket missed while disconnected.
* ``fetch_token`` — authoritative metadata, in particular ``created_timestamp``,
  which the WebSocket creation event does not carry.
"""

from __future__ import annotations

import logging
from typing import Any

from hades.providers.errors import ProviderSchemaError
from hades.providers.http import ProviderHttpClient
from hades.providers.models import DiscoveredToken, epoch_ms_to_datetime

logger = logging.getLogger(__name__)

SOURCE = "pumpfun"
BASE_URL = "https://frontend-api-v3.pump.fun"


class PumpFunProvider:
    """Read-only client for pump.fun's frontend API."""

    def __init__(self, client: ProviderHttpClient | None = None) -> None:
        self._client = client or ProviderHttpClient(SOURCE, base_url=BASE_URL)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[DiscoveredToken]:
        """Newest tokens first.

        Note the measured scale: at ~0.24 creations/s, 50 tokens covers barely
        three minutes. This is a backfill and a safety net, not a substitute for
        the push stream.
        """
        payload = await self._client.get_json(
            "/coins",
            params={
                "offset": offset,
                "limit": limit,
                "sort": "created_timestamp",
                "order": "DESC",
                "includeNsfw": "true",
            },
        )
        items = _coerce_list(payload)

        tokens: list[DiscoveredToken] = []
        for item in items:
            token = self._parse(item, strict=False)
            if token is not None:
                tokens.append(token)

        if items and not tokens:
            # Every item failing is a contract change, not bad luck.
            raise ProviderSchemaError(
                SOURCE, f"/coins returned {len(items)} items and none could be parsed"
            )
        return tokens

    async def fetch_token(self, token_address: str) -> DiscoveredToken:
        """Authoritative record for one mint. Raises on a schema mismatch."""
        payload = await self._client.get_json(f"/coins/{token_address}")
        if not isinstance(payload, dict):
            raise ProviderSchemaError(
                SOURCE, f"/coins/{token_address} returned {type(payload).__name__}, expected object"
            )
        token = self._parse(payload, strict=True)
        if token is None:  # pragma: no cover — strict=True raises instead
            raise ProviderSchemaError(SOURCE, f"/coins/{token_address} could not be parsed")
        return token

    def _parse(self, item: Any, *, strict: bool) -> DiscoveredToken | None:
        """Map one pump.fun coin object onto DiscoveredToken.

        ``strict`` distinguishes the two call sites: a single fetch we asked for
        by mint must succeed or raise, while one malformed entry in a list of 50
        should not discard the other 49.
        """
        try:
            if not isinstance(item, dict):
                msg = f"expected object, got {type(item).__name__}"
                raise ValueError(msg)

            mint = item.get("mint")
            if not isinstance(mint, str):
                msg = f"missing or non-string 'mint': {mint!r}"
                raise ValueError(msg)

            created_at = None
            raw_created = item.get("created_timestamp")
            if isinstance(raw_created, (int, float)) and not isinstance(raw_created, bool):
                created_at = epoch_ms_to_datetime(raw_created, field="created_timestamp")

            return DiscoveredToken(
                token_address=mint,
                symbol=_as_str(item.get("symbol")),
                name=_as_str(item.get("name")),
                creator_address=_as_str(item.get("creator")),
                created_at=created_at,
                source=SOURCE,
            )
        except ValueError as exc:
            if strict:
                raise ProviderSchemaError(SOURCE, str(exc)) from exc
            logger.warning(
                "provider_item_skipped",
                extra={"context": {"provider": SOURCE, "reason": str(exc)}},
            )
            return None


def _coerce_list(payload: Any) -> list[Any]:
    """``/coins`` has returned both a bare array and an envelope. Accept both."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("coins", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ProviderSchemaError(SOURCE, f"/coins returned unusable shape {type(payload).__name__}")


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
