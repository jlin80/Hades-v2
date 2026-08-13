"""Provider parsing and HTTP behaviour.

Providers are driven through a mock transport rather than the live network, so
these tests are deterministic — but the fixtures are real captured responses,
so the parsers are tested against the shape they will actually meet.
"""

import json
from typing import Any

import httpx2
import pytest

from hades.discovery.errors import ProviderError, ProviderSchemaError
from hades.discovery.http import get_json
from hades.discovery.providers.dexscreener import DexScreenerProvider
from hades.discovery.providers.geckoterminal import GeckoTerminalProvider
from tests.fixtures import DEXSCREENER_RESPONSE, GECKOTERMINAL_RESPONSE

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
DEX_BASE = "https://api.dexscreener.com"


def client_returning(*responses: httpx2.Response) -> tuple[httpx2.AsyncClient, list[int]]:
    """Return a client that replays ``responses`` in order, and a call counter."""
    calls = [0]
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls[0] += 1
        return queue.pop(0) if len(queue) > 1 else queue[0]

    transport = httpx2.MockTransport(handler)
    return httpx2.AsyncClient(transport=transport), calls


def json_response(status_code: int, payload: Any, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return httpx2.Response(status_code, content=json.dumps(payload), headers=headers)


# --- GeckoTerminal -----------------------------------------------------------


async def test_geckoterminal_parses_a_real_pool() -> None:
    client, _ = client_returning(json_response(200, GECKOTERMINAL_RESPONSE))
    async with client:
        tokens = await GeckoTerminalProvider(client, GECKO_BASE, 0).discover_tokens()

    assert len(tokens) == 1
    token = tokens[0]
    # The base token's mint, extracted from the "solana_<address>" relationship.
    assert token.token_address == "9BsHRRVeCkKhLcTStBnUcHBmqssJgcbEcphgcopump"
    assert token.symbol == "TRENCHTOK"
    assert token.pool_address == "3tDxSsENwvF4tXS81KZjVJnCNf1GhBAaQhnqLxWbJJ8T"
    assert token.first_seen_at is not None
    assert token.first_seen_at.tzinfo is not None
    assert token.provider_name == "geckoterminal"
    # The whole pool object is retained so a schema change stays detectable.
    assert token.raw["attributes"]["fdv_usd"] == "2135.741588"


async def test_geckoterminal_rejects_an_unexpected_envelope() -> None:
    """A changed response shape must be loud, not silently empty."""
    client, _ = client_returning(json_response(200, {"unexpected": []}))
    async with client:
        with pytest.raises(ProviderSchemaError) as exc_info:
            await GeckoTerminalProvider(client, GECKO_BASE, 0).discover_tokens()

    assert exc_info.value.provider == "geckoterminal"
    assert exc_info.value.endpoint == "/networks/solana/new_pools"


async def test_geckoterminal_skips_unparseable_pools_without_failing() -> None:
    """One malformed entry must not discard the whole batch."""
    payload = {"data": [{"garbage": True}, *GECKOTERMINAL_RESPONSE["data"]]}
    client, _ = client_returning(json_response(200, payload))
    async with client:
        tokens = await GeckoTerminalProvider(client, GECKO_BASE, 0).discover_tokens()

    assert len(tokens) == 1


async def test_geckoterminal_returns_no_symbol_rather_than_guessing() -> None:
    """A pool name that is not 'BASE / QUOTE' yields None, not a bad symbol."""
    pool = json.loads(json.dumps(GECKOTERMINAL_RESPONSE))
    pool["data"][0]["attributes"]["name"] = "weird-name-without-separator"
    client, _ = client_returning(json_response(200, pool))
    async with client:
        tokens = await GeckoTerminalProvider(client, GECKO_BASE, 0).discover_tokens()

    assert tokens[0].symbol is None


# --- DexScreener -------------------------------------------------------------


async def test_dexscreener_keeps_only_solana_entries() -> None:
    """The feed is cross-chain; a Base token must never enter a Solana dataset."""
    client, _ = client_returning(json_response(200, DEXSCREENER_RESPONSE))
    async with client:
        tokens = await DexScreenerProvider(client, DEX_BASE, 0).discover_tokens()

    assert len(tokens) == 1
    assert tokens[0].token_address == "EwAmHqXTzWsHdKZSCengDu15SM6ZyurDd2mrZhLBpump"
    # This endpoint carries no ticker or pool age; both stay null.
    assert tokens[0].symbol is None
    assert tokens[0].first_seen_at is None


# --- HTTP behaviour ----------------------------------------------------------


async def test_rate_limit_is_retried_then_succeeds() -> None:
    client, calls = client_returning(
        json_response(429, {"error": "slow down"}, {"Retry-After": "0"}),
        json_response(200, {"ok": True}),
    )
    async with client:
        payload = await get_json(
            client, provider="p", url="https://example.test/x", endpoint="/x", max_retries=2
        )

    assert payload == {"ok": True}
    assert calls[0] == 2


async def test_retries_are_bounded_and_failure_is_attributable() -> None:
    """Exhausted retries must name provider, endpoint, status and retry count."""
    client, calls = client_returning(json_response(503, {"error": "down"}))
    async with client:
        with pytest.raises(ProviderError) as exc_info:
            await get_json(
                client, provider="p", url="https://example.test/x", endpoint="/x", max_retries=2
            )

    error = exc_info.value
    assert calls[0] == 3  # the first attempt plus two retries, and no more
    assert error.provider == "p"
    assert error.endpoint == "/x"
    assert error.status_code == 503
    assert error.retry_count == 2
    assert set(error.as_log_fields()) >= {
        "provider",
        "endpoint",
        "error_type",
        "status_code",
        "retry_count",
    }


async def test_client_error_is_not_retried() -> None:
    """A 404 means the request is wrong; retrying only burns the rate limit."""
    client, calls = client_returning(json_response(404, {"error": "route not found"}))
    async with client:
        with pytest.raises(ProviderError) as exc_info:
            await get_json(
                client, provider="p", url="https://example.test/x", endpoint="/x", max_retries=3
            )

    assert calls[0] == 1
    assert exc_info.value.status_code == 404


async def test_transport_failure_is_reported_not_swallowed() -> None:
    """This is the class of failure that silently killed v1 when a host died."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("nodename nor servname provided")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc_info:
            await get_json(
                client, provider="p", url="https://dead.test/x", endpoint="/x", max_retries=0
            )

    assert exc_info.value.error_type == "ConnectError"
    assert exc_info.value.status_code is None


async def test_non_json_response_is_a_schema_error() -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _r: httpx2.Response(200, content=b"<html>nope"))
    ) as client:
        with pytest.raises(ProviderSchemaError):
            await get_json(
                client, provider="p", url="https://example.test/x", endpoint="/x", max_retries=0
            )
