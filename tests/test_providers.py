"""Provider parsing and HTTP policy, against recorded shapes and a fake transport.

The payloads here are the real field sets captured during Phase 1, not
invented ones — see docs/DATA_SOURCES.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from hades.providers.errors import (
    ProviderRateLimitedError,
    ProviderSchemaError,
    ProviderUnavailableError,
)
from hades.providers.http import ProviderHttpClient
from hades.providers.pumpfun import PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider

MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"
CREATOR = "8i6qTrvQZ2c66GdPb8CgQh599CAMUGJPWqVWMwbtjGYf"

# Captured from frontend-api-v3 on 2026-08-17, trimmed to the fields we read.
PUMPFUN_COIN: dict[str, Any] = {
    "mint": MINT,
    "name": "Clown of the United States",
    "symbol": "COTUS",
    "creator": CREATOR,
    "created_timestamp": 1786973934000,
    "market_cap": 104.60510633518983,
    "usd_market_cap": 7905.9820717484145,
    "virtual_sol_reserves": 58027910356,
    "virtual_token_reserves": 554733056434731,
    "real_sol_reserves": 28027910356,
    "complete": False,
    "updated_at": 1786973986,
}

# Captured from PumpPortal subscribeNewToken on 2026-08-17.
PUMPPORTAL_CREATE: dict[str, Any] = {
    "signature": (
        "38acc7hviz2UjaYTbE2eVSowkv8Vsuf8pvF4BKAuhV8REiXu5CkjXZhsc8xD8hqzzCsfHYKpxpdPStfgieJSamHd"
    ),
    "mint": MINT,
    "traderPublicKey": CREATOR,
    "txType": "create",
    "name": "The Unicorn",
    "symbol": "UNICORN",
    "marketCapSol": 33.75448670917235,
    "vSolInBondingCurve": 32.962962961,
    "vTokensInBondingCurve": 976550561.855907,
    "initialBuy": 96449438.144093,
    "solAmount": 2.962962961,
    "bondingCurveKey": "53eum63MmmmKaHkohpDzpGHGWi4C5FkTUNuCCMgXTsAD",
    "pool": "pump",
    "uri": "https://m.rapidlaunch.io/m/nsPulgwkX",
    "is_mayhem_mode": False,
}


def client_with(handler: Any, **kwargs: Any) -> ProviderHttpClient:
    """A ProviderHttpClient whose transport is a fake."""
    client = ProviderHttpClient("test", base_url="https://example.invalid", **kwargs)
    client._client = httpx.AsyncClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(handler),
    )
    return client


class TestPumpFunParsing:
    async def test_fetch_token_maps_the_real_payload(self) -> None:
        provider = PumpFunProvider(client_with(lambda _: httpx.Response(200, json=PUMPFUN_COIN)))
        token = await provider.fetch_token(MINT)

        assert token.token_address == MINT
        assert token.symbol == "COTUS"
        assert token.creator_address == CREATOR
        assert token.created_at == datetime(2026, 8, 17, 13, 38, 54, tzinfo=UTC)
        assert token.source == "pumpfun"

    async def test_list_recent_accepts_a_bare_array(self) -> None:
        provider = PumpFunProvider(
            client_with(lambda _: httpx.Response(200, json=[PUMPFUN_COIN, PUMPFUN_COIN]))
        )
        assert len(await provider.list_recent()) == 2

    async def test_list_recent_accepts_an_envelope(self) -> None:
        """The endpoint has returned both shapes; accepting one would be brittle."""
        provider = PumpFunProvider(
            client_with(lambda _: httpx.Response(200, json={"coins": [PUMPFUN_COIN]}))
        )
        assert len(await provider.list_recent()) == 1

    async def test_one_bad_item_does_not_discard_the_good_ones(self) -> None:
        payload = [PUMPFUN_COIN, {"mint": None}, {"not": "a coin"}]
        provider = PumpFunProvider(client_with(lambda _: httpx.Response(200, json=payload)))
        tokens = await provider.list_recent()
        assert len(tokens) == 1

    async def test_all_items_failing_raises_schema_error(self) -> None:
        """Every item failing is a contract change, not bad luck.

        Returning an empty list here would look exactly like a quiet period and
        the dataset would just stop growing, unexplained.
        """
        provider = PumpFunProvider(
            client_with(lambda _: httpx.Response(200, json=[{"x": 1}, {"y": 2}]))
        )
        with pytest.raises(ProviderSchemaError, match="none could be parsed"):
            await provider.list_recent()

    async def test_unusable_top_level_shape_raises(self) -> None:
        provider = PumpFunProvider(client_with(lambda _: httpx.Response(200, json="nope")))
        with pytest.raises(ProviderSchemaError):
            await provider.list_recent()

    async def test_fetch_token_rejects_a_seconds_timestamp(self) -> None:
        """A wrong-unit timestamp must fail, not become a 100-year-old token."""
        payload = PUMPFUN_COIN | {"created_timestamp": 1786973934}
        provider = PumpFunProvider(client_with(lambda _: httpx.Response(200, json=payload)))
        with pytest.raises(ProviderSchemaError, match="wrong unit"):
            await provider.fetch_token(MINT)

    async def test_missing_timestamp_is_null_not_an_error(self) -> None:
        payload = {k: v for k, v in PUMPFUN_COIN.items() if k != "created_timestamp"}
        provider = PumpFunProvider(client_with(lambda _: httpx.Response(200, json=payload)))
        token = await provider.fetch_token(MINT)
        assert token.created_at is None


class TestHttpPolicy:
    async def test_retries_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        client = client_with(handler, backoff_base_seconds=0.0)
        assert await client.get_json("/x") == {"ok": True}
        assert calls["n"] == 3

    async def test_exhausted_attempts_raise_unavailable(self) -> None:
        client = client_with(lambda _: httpx.Response(500), backoff_base_seconds=0.0)
        with pytest.raises(ProviderUnavailableError, match="attempts exhausted"):
            await client.get_json("/x")

    async def test_429_raises_immediately_with_retry_after(self) -> None:
        """Never retried in place: the provider asked us to stop, so we stop."""
        client = client_with(
            lambda _: httpx.Response(429, headers={"retry-after": "12"}),
            backoff_base_seconds=0.0,
        )
        with pytest.raises(ProviderRateLimitedError) as caught:
            await client.get_json("/x")
        assert caught.value.retry_after_seconds == 12.0

    async def test_unparseable_retry_after_is_none_not_a_guess(self) -> None:
        client = client_with(
            lambda _: httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            backoff_base_seconds=0.0,
        )
        with pytest.raises(ProviderRateLimitedError) as caught:
            await client.get_json("/x")
        assert caught.value.retry_after_seconds is None

    async def test_404_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404)

        client = client_with(handler, backoff_base_seconds=0.0)
        with pytest.raises(ProviderUnavailableError, match="404"):
            await client.get_json("/x")
        assert calls["n"] == 1

    async def test_200_with_non_json_body_is_an_error(self) -> None:
        client = client_with(
            lambda _: httpx.Response(200, text="<html>cloudflare</html>"),
            backoff_base_seconds=0.0,
        )
        with pytest.raises(ProviderUnavailableError, match="not JSON"):
            await client.get_json("/x")

    async def test_connection_limits_are_explicit(self) -> None:
        """V1 had none anywhere, and read the resulting timeouts as provider faults."""
        client = ProviderHttpClient("t", base_url="https://example.invalid", max_connections=7)
        assert client.limits.max_connections == 7
        assert client.limits.max_keepalive_connections == 3
        await client.aclose()


class TestPumpPortalParsing:
    def test_create_frame_maps_to_token(self) -> None:
        provider = PumpPortalProvider()
        token = provider._parse(json.dumps(PUMPPORTAL_CREATE))
        assert token is not None
        assert token.token_address == MINT
        assert token.creator_address == CREATOR
        assert token.source == "pumpportal"
        assert token.raw_provider_reference == PUMPPORTAL_CREATE["signature"]

    def test_created_at_is_none_not_arrival_time(self) -> None:
        """The creation event has no timestamp, and we must not invent one.

        Stamping arrival time would make discovery latency measure as ~0 forever
        — destroying the exact number CREATED vs DISCOVERED exists to expose.
        """
        provider = PumpPortalProvider()
        token = provider._parse(json.dumps(PUMPPORTAL_CREATE))
        assert token is not None
        assert token.created_at is None

    def test_server_message_frame_is_not_a_token(self) -> None:
        provider = PumpPortalProvider()
        frame = {"message": "... only available when connecting with an API key funded ..."}
        assert provider._parse(json.dumps(frame)) is None

    def test_trade_frame_is_ignored(self) -> None:
        provider = PumpPortalProvider()
        frame = PUMPPORTAL_CREATE | {"txType": "buy"}
        assert provider._parse(json.dumps(frame)) is None

    def test_non_json_frame_is_ignored(self) -> None:
        provider = PumpPortalProvider()
        assert provider._parse("not json at all") is None

    def test_frame_without_mint_is_ignored(self) -> None:
        provider = PumpPortalProvider()
        frame = {k: v for k, v in PUMPPORTAL_CREATE.items() if k != "mint"}
        assert provider._parse(json.dumps(frame)) is None
