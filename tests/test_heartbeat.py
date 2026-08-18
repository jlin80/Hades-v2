"""The hourly Discord heartbeat: independent of trade events, fires on a clock.

Mirrors the paper-trade notifier's contract -- best-effort, never load-bearing
-- and additionally checks that a report is actually produced against real
subsystem tables, not just that the notifier is called.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.config import Settings
from hades.db.base import Base
from hades.monitoring.heartbeat import DiscordHeartbeat, HeartbeatService, build_heartbeat_service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class FakeDatabase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


def notifier_with(handler) -> DiscordHeartbeat:  # type: ignore[no-untyped-def]
    notifier = DiscordHeartbeat("https://discord.example.invalid/webhook")
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return notifier


async def test_reports_zeroed_stats_on_an_empty_database(session: AsyncSession) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    service = HeartbeatService(
        FakeDatabase(session),  # type: ignore[arg-type]
        notifier_with(handler),
        settings=Settings(),
        paper_service=None,
        outcome_service=None,
        interval_seconds=3600.0,
    )
    await service._report_once()

    fields = {f["name"]: f["value"] for f in captured["body"]["embeds"][0]["fields"]}
    assert fields["Tokens discovered"] == "0"
    assert fields["Balance"] == "0.0000 SOL"
    assert "0 analyses -> 0 signals -> 0 trades" in fields["Pipeline totals"]


async def test_a_report_failure_does_not_raise(session: AsyncSession) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    service = HeartbeatService(
        FakeDatabase(session),  # type: ignore[arg-type]
        notifier_with(handler),
        settings=Settings(),
        paper_service=None,
        outcome_service=None,
        interval_seconds=3600.0,
    )
    await service._report_once()  # must not raise


def test_a_missing_webhook_builds_no_service() -> None:
    settings = Settings(discord_webhook_url=None)

    class DummyDatabase:
        pass

    assert build_heartbeat_service(DummyDatabase(), settings, None, None) is None  # type: ignore[arg-type]


def test_a_zero_interval_disables_the_heartbeat() -> None:
    settings = Settings(
        discord_webhook_url="https://discord.example.invalid/webhook",
        discord_status_interval_seconds=0,
    )

    class DummyDatabase:
        pass

    assert build_heartbeat_service(DummyDatabase(), settings, None, None) is None  # type: ignore[arg-type]


class TestResearchReadyPing:
    """The one notification worth an @everyone -- fires once, when it should."""

    async def test_pings_everyone_once_the_threshold_is_reached(
        self, session: AsyncSession
    ) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(204)

        class StubOutcomeService:
            async def signalled_final_count(self) -> int:
                return 150

        service = HeartbeatService(
            FakeDatabase(session),  # type: ignore[arg-type]
            notifier_with(handler),
            settings=Settings(research_ready_threshold=100),
            paper_service=None,
            outcome_service=StubOutcomeService(),  # type: ignore[arg-type]
            interval_seconds=3600.0,
        )
        await service._report_once()

        assert len(captured) == 2  # the regular status post, then the ping
        ping = captured[1]
        assert ping["content"] == "@everyone"
        assert ping["allowed_mentions"] == {"parse": ["everyone"]}
        embed = ping["embeds"][0]  # type: ignore[index]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Señales con outcome final"] == "150"

    async def test_does_not_ping_below_the_threshold(self, session: AsyncSession) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(204)

        class StubOutcomeService:
            async def signalled_final_count(self) -> int:
                return 40

        service = HeartbeatService(
            FakeDatabase(session),  # type: ignore[arg-type]
            notifier_with(handler),
            settings=Settings(research_ready_threshold=100),
            paper_service=None,
            outcome_service=StubOutcomeService(),  # type: ignore[arg-type]
            interval_seconds=3600.0,
        )
        await service._report_once()

        assert len(captured) == 1  # only the regular status post

    async def test_pings_only_once_across_repeated_passes(self, session: AsyncSession) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(204)

        class StubOutcomeService:
            async def signalled_final_count(self) -> int:
                return 150

        service = HeartbeatService(
            FakeDatabase(session),  # type: ignore[arg-type]
            notifier_with(handler),
            settings=Settings(research_ready_threshold=100),
            paper_service=None,
            outcome_service=StubOutcomeService(),  # type: ignore[arg-type]
            interval_seconds=3600.0,
        )
        await service._report_once()
        await service._report_once()

        pings = [body for body in captured if body.get("content") == "@everyone"]
        assert len(pings) == 1
