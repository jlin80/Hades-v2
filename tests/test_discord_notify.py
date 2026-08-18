"""The Discord notifier: best-effort, and never load-bearing.

A signal is already durable in Postgres the moment it is recorded. This module
only mirrors that fact to a human faster than polling /status would, so every
test here is really about one property: nothing it does can affect the signal
pipeline, however Discord behaves.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from hades.signals.models import Condition, Signal
from hades.signals.notify import DISCLAIMER, DiscordNotifier

MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"


def make_signal(**conditions: bool) -> Signal:
    return Signal(
        token_address=MINT,
        strategy="early_momentum",
        strategy_version="1.0.0",
        created_at=datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC),
        conditions=tuple(
            Condition(name=name, passed=passed, value=1.0, threshold=0.5, description="d")
            for name, passed in conditions.items()
        ),
    )


def notifier_with(handler) -> DiscordNotifier:  # type: ignore[no-untyped-def]
    notifier = DiscordNotifier("https://discord.example.invalid/webhook")
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return notifier


class TestSendsOnSuccess:
    async def test_posts_to_the_configured_url(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        await notifier.send_signal(make_signal(liquidity=True), {"market_cap_sol": 30.0})
        await notifier.aclose()

        assert len(calls) == 1
        assert str(calls[0].url) == "https://discord.example.invalid/webhook"

    async def test_the_payload_carries_the_disclaimer(self) -> None:
        """The one surface a human reads without going through /status.

        It is also the one place a reader could mistake a signal for an
        instruction if the disclaimer were left off.
        """
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        await notifier.send_signal(make_signal(liquidity=True), {})
        await notifier.aclose()

        footer = captured["body"]["embeds"][0]["footer"]["text"]
        assert footer == DISCLAIMER
        assert "no order" in footer.lower() or "no signer" in footer.lower()

    async def test_conditions_are_rendered_pass_and_fail(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        notifier = notifier_with(handler)
        signal = make_signal(liquidity=True, token_age=False)
        await notifier.send_signal(signal, {})
        await notifier.aclose()

        conditions_field = next(
            f for f in captured["body"]["embeds"][0]["fields"] if f["name"] == "Conditions"
        )
        assert "✅ liquidity" in conditions_field["value"]
        assert "❌ token_age" in conditions_field["value"]

    async def test_missing_feature_values_render_as_a_dash_not_a_crash(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        notifier = notifier_with(handler)
        # No exception, even though every value is absent.
        await notifier.send_signal(make_signal(x=True), {})
        await notifier.aclose()


class TestNeverBreaksThePipeline:
    """The property that matters most: a Discord outage cannot stall signals."""

    async def test_a_4xx_response_is_swallowed(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        notifier = notifier_with(handler)
        await notifier.send_signal(make_signal(x=True), {})  # must not raise
        await notifier.aclose()

    async def test_a_5xx_response_is_swallowed(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        notifier = notifier_with(handler)
        await notifier.send_signal(make_signal(x=True), {})
        await notifier.aclose()

    async def test_a_transport_failure_is_swallowed(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom")

        notifier = notifier_with(handler)
        await notifier.send_signal(make_signal(x=True), {})  # must not raise
        await notifier.aclose()

    async def test_there_is_no_retry(self) -> None:
        """A retry could stack behind a webhook rate limit; a duplicate post on
        retry would be worse than a missed one, since the database row is the
        record either way."""
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500)

        notifier = notifier_with(handler)
        await notifier.send_signal(make_signal(x=True), {})
        await notifier.aclose()

        assert calls["n"] == 1


def test_a_missing_webhook_builds_no_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent, not degraded -- matching every other optional integration here."""
    from hades.config import Settings
    from hades.signals.runtime import build_signal_service

    monkeypatch.delenv("HADES_DISCORD_WEBHOOK_URL", raising=False)
    settings = Settings(discord_webhook_url=None)

    class DummyDatabase:
        pass

    service = build_signal_service(DummyDatabase(), settings)  # type: ignore[arg-type]
    assert service._notifier is None
