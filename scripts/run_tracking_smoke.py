"""Discovery plus tracking, end to end against the live sources.

Spec §22: run the system with real data after each phase, not just the tests.
Writes to a throwaway SQLite file, never to Postgres.

    python scripts/run_tracking_smoke.py [seconds]

What it proves: tokens flow from the WebSocket into the database, get admitted
to tracking within capacity, and accumulate a real time series with derived
prices. What it does not prove: Postgres behaviour, or anything about a run
longer than the window given.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import MarketSnapshot as SnapshotRow
from hades.db.models import Token
from hades.discovery.service import DiscoveryService
from hades.logging import configure_logging
from hades.providers.pumpfun import PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider
from hades.tracking.repository import TrackingRepository
from hades.tracking.schedule import TrackingSchedule
from hades.tracking.service import TrackingService


class FileDatabase:
    def __init__(self, path: Path) -> None:
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self._maker = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._maker() as active:
            yield active

    async def dispose(self) -> None:
        await self._engine.dispose()


async def main(seconds: float) -> int:
    configure_logging("WARNING")  # the interesting output is the report below
    directory = Path(tempfile.mkdtemp(prefix="hades-track-"))
    database = FileDatabase(directory / "smoke.db")
    await database.create_schema()

    schedule = TrackingSchedule()
    discovery = DiscoveryService(
        database,  # type: ignore[arg-type]
        pumpfun=PumpFunProvider(),
        pumpportal=PumpPortalProvider(),
        poll_interval_seconds=30.0,
        poll_limit=25,
    )
    tracking = TrackingService(
        database,  # type: ignore[arg-type]
        pumpfun=PumpFunProvider(),
        schedule=schedule,
        max_concurrent=10,
        batch_size=10,
        pass_interval_seconds=2.0,
    )

    print(f"running discovery + tracking for {seconds:.0f}s against live sources")
    print(f"database: {directory / 'smoke.db'}")
    print(f"capacity: {tracking.estimated_requests_per_second():.2f} req/s estimated\n")

    tasks = [
        asyncio.create_task(discovery.run(), name="discovery"),
        asyncio.create_task(tracking.run(), name="tracking"),
    ]
    await asyncio.sleep(seconds)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
            await task
    await discovery.aclose()
    await tracking.aclose()

    async with database.session() as session:
        stats = await TrackingRepository(session, schedule).stats()
        by_tier = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    select(SnapshotRow.tier, func.count()).group_by(SnapshotRow.tier)
                )
            ).all()
        }
        # The token with the longest series, and what it actually looks like.
        best = (
            await session.execute(
                select(Token.token_address, Token.symbol, Token.snapshot_count)
                .where(Token.snapshot_count > 0)
                .order_by(Token.snapshot_count.desc())
                .limit(1)
            )
        ).first()
        series: list[tuple[float, float, float]] = []
        if best is not None:
            series = [
                (row[0], row[1], row[2])
                for row in (
                    await session.execute(
                        select(
                            SnapshotRow.token_age_seconds,
                            SnapshotRow.price_sol,
                            SnapshotRow.market_cap_sol,
                        )
                        .join(Token, Token.id == SnapshotRow.token_id)
                        .where(Token.token_address == best[0])
                        .order_by(SnapshotRow.observed_at)
                    )
                ).all()
                if row[0] is not None
            ]

    print("=" * 84)
    print("DISCOVERY")
    for key, value in discovery.counters.as_dict().items():
        print(f"  {key:<24} {value}")

    print("\nTRACKING")
    for key, value in tracking.counters.as_dict().items():
        print(f"  {key:<24} {value}")

    print("\nDATABASE (source of truth)")
    print(f"  tracking_now             {stats.tracking_now} / 10 capacity")
    print(f"  eligible_waiting         {stats.eligible_waiting}   <- sample we declined to take")
    print(f"  snapshots_total          {stats.snapshots_total}")
    print(f"  stale_snapshots          {stats.stale_snapshots}")
    print(f"  tokens_migrated          {stats.tokens_migrated}")
    print(f"  tokens_dead              {stats.tokens_dead}")
    print(f"  oldest_due_seconds       {stats.oldest_due_seconds}   <- >0 means behind")
    print(f"  snapshots by tier        {by_tier}")

    if best is not None:
        print(f"\nLONGEST SERIES: {best[1]} ({best[0][:16]}...), {best[2]} snapshots")
        print(f"  {'age_s':>8}  {'price_sol':>14}  {'mcap_sol':>10}")
        for age, price, mcap in series[:12]:
            print(f"  {age:>8.1f}  {price:>14.3e}  {mcap:>10.3f}")

    await database.dispose()

    if stats.snapshots_total == 0:
        print("\nNo snapshots persisted. That is a failure, not a quiet market.")
        return 1
    print(f"\nOK: {stats.snapshots_total} snapshots from live sources.")
    return 0


if __name__ == "__main__":
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    sys.exit(asyncio.run(main(window)))
