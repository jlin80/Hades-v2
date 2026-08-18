"""Discovery, tracking and signal research together, against live sources.

Spec §22: run the system with real data after each phase. Writes to a throwaway
SQLite file, never to Postgres.

    python scripts/run_signals_smoke.py [seconds]

**No orders are produced.** A signal here is a research observation, and nothing
in this repository can execute anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import FeatureObservation, SignalRow
from hades.discovery.service import DiscoveryService
from hades.logging import configure_logging
from hades.providers.pumpfun import PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider
from hades.signals.early_momentum import EarlyMomentumStrategy
from hades.signals.models import MarketState
from hades.signals.repository import SignalRepository
from hades.signals.service import SignalService
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
    configure_logging("WARNING")
    directory = Path(tempfile.mkdtemp(prefix="hades-signals-"))
    database = FileDatabase(directory / "smoke.db")
    await database.create_schema()

    schedule = TrackingSchedule()
    strategy = EarlyMomentumStrategy()
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
        max_concurrent=12,
        batch_size=12,
        pass_interval_seconds=2.0,
    )
    signals = SignalService(
        database,  # type: ignore[arg-type]
        strategy,
        pass_interval_seconds=3.0,
    )

    print(f"running discovery + tracking + signals for {seconds:.0f}s against live sources")
    print(f"database: {directory / 'smoke.db'}")
    print(f"strategy: {strategy.name} v{strategy.version}  (research only, no orders)\n")

    tasks = [
        asyncio.create_task(discovery.run(), name="discovery"),
        asyncio.create_task(tracking.run(), name="tracking"),
        asyncio.create_task(signals.run(), name="signals"),
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
        stats = await SignalRepository(session).stats()
        observations = await session.scalar(select(func.count()).select_from(FeatureObservation))
        # Which clause blocked the most evaluations. The number that says what
        # the hypothesis is actually gated on, rather than that it did not fire.
        rows = (
            await session.execute(
                select(FeatureObservation.features).order_by(FeatureObservation.observed_at.desc())
            )
        ).all()
        fired = (await session.execute(select(SignalRow.token_address))).all()

    blocking: dict[str, int] = {}
    now = datetime.now(tz=UTC)
    for row in rows:
        # `check` reads only the feature values, so as_of is irrelevant here.
        state = MarketState(token_address="?", as_of=now, feature_version="1.0.0", features=row[0])
        for condition in strategy.check(state):
            if not condition.passed:
                blocking[condition.name] = blocking.get(condition.name, 0) + 1

    print("=" * 84)
    print("COUNTERS")
    for name, counters in (
        ("discovery", discovery.counters.as_dict()),
        ("tracking", tracking.counters.as_dict()),
        ("signals", signals.counters.as_dict()),
    ):
        print(f"  {name}:")
        for key, value in counters.items():
            print(f"    {key:<24} {value}")

    print("\nDATABASE (source of truth)")
    print(f"  observations_total       {observations}")
    print(f"  signals_total            {stats.signals_total}")
    print(f"  signal_rate              {stats.signal_rate}")
    print(f"  tokens_with_a_signal     {stats.tokens_with_a_signal}")

    if blocking:
        print("\nWHICH CLAUSE BLOCKED (over every observation)")
        for name, count in sorted(blocking.items(), key=lambda item: -item[1]):
            share = count / max(1, observations or 1)
            print(f"  {name:<28} {count:>5}  ({share:>5.1%} of observations)")

    if fired:
        print(f"\nSIGNALS: {len(fired)}")
        for row in fired[:10]:
            print(f"  {row[0]}")
    else:
        print("\nNo signals fired. That is a result, not a failure.")

    await database.dispose()

    if not observations:
        print("\nNo observations stored. That is a failure.")
        return 1
    print(f"\nOK: {observations} observations, {stats.signals_total} signals.")
    return 0


if __name__ == "__main__":
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    sys.exit(asyncio.run(main(window)))
