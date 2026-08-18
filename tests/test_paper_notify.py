"""``PaperDiscordNotifier``: same best-effort contract as the signal notifier.

A paper trade is already durable in Postgres the moment it fills or closes, so
these tests are about one property: nothing here can affect the paper-trading
loop, however Discord behaves.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hades.paper.notify import PaperDiscordNotifier
from hades.signals.notify import DISCLAIMER


def notifier_with(handler) -> PaperDiscordNotifier:  # type: ignore[no-untyped-def]
    notifier = PaperDiscordNotifier("https://discord.example.invalid/webhook")
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return notifier


OPENED_KWARGS: dict[str, Any] = {
    "token_address": "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump",
    "token_symbol": "HADES",
    "position_size_sol": 0.02,
    "entry_price_sol": 1.5e-6,
    "entry_slippage": 0.01,
    "market_cap_sol": 100.0,
    "market_cap_usd": 15000.0,
    "sol_price_usd": 150.0,
    "balance_sol": 0.98,
    "equity_sol": 1.0,
    "open_positions": 1,
    "observations_total": 500,
    "signals_total": 12,
    "trades_total": 3,
}

CLOSED_KWARGS: dict[str, Any] = {
    "token_address": "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump",
    "token_symbol": "HADES",
    "exit_reason": "take_profit",
    "net_pnl_sol": 0.005,
    "fees_sol": 0.0004,
    "balance_sol": 1.005,
    "equity_sol": 1.005,
}


class TestSendTradeOpened:
    async def test_posts_to_the_configured_url(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        await notifier.send_trade_opened(**OPENED_KWARGS)
        await notifier.aclose()

        assert len(calls) == 1
        assert str(calls[0].url) == "https://discord.example.invalid/webhook"

    async def test_reports_position_size_in_sol_and_usd(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        await notifier.send_trade_opened(**OPENED_KWARGS)
        await notifier.aclose()

        fields = {f["name"]: f["value"] for f in captured["body"]["embeds"][0]["fields"]}
        assert "0.0200 SOL" in fields["Position size"]
        assert "$3.00" in fields["Position size"]
        assert "Market cap at entry" in fields
        assert "$15,000.00" in fields["Market cap at entry"]
        assert "500 analyses -> 12 signals -> 3 trades" in fields["Pipeline so far"]

    async def test_missing_sol_price_renders_without_usd(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        kwargs = dict(OPENED_KWARGS, sol_price_usd=None)
        notifier = notifier_with(handler)
        await notifier.send_trade_opened(**kwargs)
        await notifier.aclose()

        fields = {f["name"]: f["value"] for f in captured["body"]["embeds"][0]["fields"]}
        assert fields["Position size"] == "0.0200 SOL"

    async def test_carries_the_disclaimer(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        await notifier.send_trade_opened(**OPENED_KWARGS)
        await notifier.aclose()

        assert captured["body"]["embeds"][0]["footer"]["text"] == DISCLAIMER


class TestSendTradeClosed:
    async def test_reports_net_pnl_and_balance(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        await notifier.send_trade_closed(**CLOSED_KWARGS)
        await notifier.aclose()

        fields = {f["name"]: f["value"] for f in captured["body"]["embeds"][0]["fields"]}
        assert fields["Net PnL"] == "+0.00500 SOL"
        assert fields["Balance now"] == "1.0050 SOL"

    async def test_a_loss_uses_the_loss_color(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        await notifier.send_trade_closed(**dict(CLOSED_KWARGS, net_pnl_sol=-0.003))
        await notifier.aclose()

        embed = captured["body"]["embeds"][0]
        assert embed["color"] == 0xED4245
        assert "-0.00300 SOL" in embed["fields"][2]["value"]


class TestNeverBreaksThePipeline:
    async def test_a_4xx_response_is_swallowed(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        notifier = notifier_with(handler)
        await notifier.send_trade_opened(**OPENED_KWARGS)  # must not raise
        await notifier.aclose()

    async def test_a_transport_failure_is_swallowed(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom")

        notifier = notifier_with(handler)
        await notifier.send_trade_closed(**CLOSED_KWARGS)  # must not raise
        await notifier.aclose()

    async def test_there_is_no_retry(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500)

        notifier = notifier_with(handler)
        await notifier.send_trade_opened(**OPENED_KWARGS)
        await notifier.aclose()

        assert calls["n"] == 1


def test_a_missing_webhook_builds_no_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from hades.config import Settings
    from hades.paper.runtime import build_paper_service

    monkeypatch.delenv("HADES_DISCORD_WEBHOOK_URL", raising=False)
    settings = Settings(discord_webhook_url=None)

    class DummyDatabase:
        pass

    service = build_paper_service(DummyDatabase(), settings)  # type: ignore[arg-type]
    assert service._notifier is None
