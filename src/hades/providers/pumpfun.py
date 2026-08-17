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
from datetime import UTC, datetime
from typing import Any

from hades.providers.errors import ProviderSchemaError
from hades.providers.http import ProviderHttpClient
from hades.providers.models import DiscoveredToken, MarketSnapshot, epoch_ms_to_datetime
from hades.tracking.derive import derive_market, derive_sol_price_usd

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

    async def fetch_snapshot(self, token_address: str) -> MarketSnapshot:
        """One market observation for a mint.

        Same endpoint as ``fetch_token`` — pump.fun returns identity and market
        state in one payload — but a different projection of it. Kept separate
        because the two have different failure meanings: a 404 during discovery
        means "not indexed yet", while a 404 during tracking means the token has
        stopped being available and tracking should react.
        """
        received_at = datetime.now(tz=UTC)
        payload = await self._client.get_json(f"/coins/{token_address}")
        if not isinstance(payload, dict):
            raise ProviderSchemaError(
                SOURCE, f"/coins/{token_address} returned {type(payload).__name__}, expected object"
            )

        virtual_sol = _as_int(payload.get("virtual_sol_reserves"))
        virtual_token = _as_int(payload.get("virtual_token_reserves"))
        real_sol = _as_int(payload.get("real_sol_reserves"))
        total_supply = _as_int(payload.get("total_supply"))
        base_decimals = _as_int(payload.get("base_decimals"))
        quote_decimals = _as_int(payload.get("quote_decimals"))

        derived = derive_market(
            virtual_sol_reserves=virtual_sol,
            virtual_token_reserves=virtual_token,
            real_sol_reserves=real_sol,
            total_supply=total_supply,
            base_decimals=base_decimals,
            quote_decimals=quote_decimals,
        )
        market_cap_usd = _as_float(payload.get("usd_market_cap"))

        return MarketSnapshot(
            token_address=token_address,
            source=SOURCE,
            # `updated_at` is in SECONDS here while `created_timestamp` and
            # `last_trade_timestamp` are in MILLISECONDS. The inconsistency is
            # the provider's; handling it explicitly per field is the only way
            # not to inherit it as a silent thousand-fold error.
            provider_updated_at=_epoch_seconds(payload.get("updated_at")),
            received_at=received_at,
            virtual_sol_reserves=virtual_sol,
            virtual_token_reserves=virtual_token,
            real_sol_reserves=real_sol,
            real_token_reserves=_as_int(payload.get("real_token_reserves")),
            total_supply=total_supply,
            base_decimals=base_decimals,
            quote_decimals=quote_decimals,
            price_sol=derived.price_sol,
            market_cap_sol=derived.market_cap_sol,
            liquidity_sol=derived.liquidity_sol,
            market_cap_usd=market_cap_usd,
            sol_price_usd=derive_sol_price_usd(derived.market_cap_sol, market_cap_usd),
            is_complete=_as_bool(payload.get("complete")),
            last_trade_at=_epoch_milliseconds(payload.get("last_trade_timestamp")),
            reply_count=_as_int(payload.get("reply_count")),
        )

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


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _is_number(value: Any) -> bool:
    # bool is a subclass of int; `complete: false` must not become 0.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_int(value: Any) -> int | None:
    return int(value) if _is_number(value) else None


def _as_float(value: Any) -> float | None:
    return float(value) if _is_number(value) else None


def _epoch_milliseconds(value: Any) -> datetime | None:
    if not _is_number(value):
        return None
    try:
        return epoch_ms_to_datetime(float(value), field="timestamp_ms")
    except ValueError:
        # An implausible timestamp is dropped rather than stored: a snapshot
        # without last_trade_at is usable, one dated 1926 corrupts every
        # age-based feature computed from it.
        logger.warning(
            "provider_timestamp_implausible",
            extra={"context": {"provider": SOURCE, "unit": "ms", "value": value}},
        )
        return None


def _epoch_seconds(value: Any) -> datetime | None:
    if not _is_number(value):
        return None
    try:
        return epoch_ms_to_datetime(float(value) * 1000, field="timestamp_s")
    except ValueError:
        logger.warning(
            "provider_timestamp_implausible",
            extra={"context": {"provider": SOURCE, "unit": "s", "value": value}},
        )
        return None
