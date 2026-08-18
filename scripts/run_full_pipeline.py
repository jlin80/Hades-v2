"""The whole pipeline against live sources, into a throwaway SQLite file.

Discovery -> tracking -> features/signals -> risk -> paper trades -> outcomes.
Spec §22: run it with real data, not just tests.

    python scripts/run_full_pipeline.py [seconds] [--keep path.db]

What it can and cannot show: at Phase 5's measured ~2.4% signal rate and a
one-hour outcome window, a short run produces observations and maybe a signal,
and **no finalised labels at all**. That is the honest shape of the thing — a
research dataset takes hours to become a research dataset. Use
``--keep`` and then ``scripts/research_report.py`` on the same file later.
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
from hades.db.models import (
    FeatureObservation,
    MarketSnapshot,
    ObservationOutcome,
    PaperTrade,
    RiskDecisionRow,
    SignalRow,
    Token,
)
from hades.discovery.service import DiscoveryService
from hades.logging import configure_logging
from hades.outcomes.service import OutcomeService
from hades.paper.service import PaperConfig, PaperTradingService
from hades.providers.pumpfun import PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider
from hades.signals.early_momentum import EarlyMomentumStrategy
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


async def count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def main(seconds: float, keep: Path | None) -> int:
    configure_logging("WARNING")
    directory = keep.parent if keep else Path(tempfile.mkdtemp(prefix="hades-full-"))
    path = keep or (directory / "pipeline.db")
    database = FileDatabase(path)
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
    )
    signals = SignalService(
        database,  # type: ignore[arg-type]
        EarlyMomentumStrategy(),
    )
    paper = PaperTradingService(database, config=PaperConfig())  # type: ignore[arg-type]
    outcomes = OutcomeService(database)  # type: ignore[arg-type]

    print(f"running the full pipeline for {seconds:.0f}s against live sources")
    print(f"database: {path}\n")

    tasks = [
        asyncio.create_task(discovery.run(), name="discovery"),
        asyncio.create_task(tracking.run(), name="tracking"),
        asyncio.create_task(signals.run(), name="signals"),
        asyncio.create_task(paper.run(), name="paper"),
        asyncio.create_task(outcomes.run(), name="outcomes"),
    ]
    await asyncio.sleep(seconds)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
            await task
    # Only the services that own an HTTP client have one to close.
    for service in (discovery, tracking):
        with contextlib.suppress(Exception):
            await service.aclose()

    async with database.session() as session:
        rows = {
            "tokens": await count(session, Token),
            "snapshots": await count(session, MarketSnapshot),
            "observations": await count(session, FeatureObservation),
            "signals": await count(session, SignalRow),
            "risk_decisions": await count(session, RiskDecisionRow),
            "paper_trades": await count(session, PaperTrade),
            "outcomes": await count(session, ObservationOutcome),
        }
        final = (
            await session.scalar(
                select(func.count())
                .select_from(ObservationOutcome)
                .where(ObservationOutcome.is_final.is_(True))
            )
            or 0
        )
        approved = (
            await session.scalar(
                select(func.count())
                .select_from(RiskDecisionRow)
                .where(RiskDecisionRow.approved.is_(True))
            )
            or 0
        )

    print("=" * 72)
    print("PIPELINE (database is the source of truth)")
    for name, value in rows.items():
        print(f"  {name:<18} {value}")
    print(f"  {'risk approved':<18} {approved}")
    print(f"  {'outcomes final':<18} {final}")

    print("\n" + "=" * 72)
    if rows["observations"] == 0:
        print("No observations. That is a failure, not a quiet market.")
        await database.dispose()
        return 1
    if final == 0:
        print("No finalised labels yet, which is expected: the default barrier needs")
        print("an hour to elapse after each observation. Re-run the report later:")
        print(f"  python scripts/research_report.py {path}")
    await database.dispose()
    print(f"\nOK. Database kept at {path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    window = float(args[0]) if args and not args[0].startswith("--") else 180.0
    keep_path = Path(args[args.index("--keep") + 1]).resolve() if "--keep" in args else None
    sys.exit(asyncio.run(main(window, keep_path)))
