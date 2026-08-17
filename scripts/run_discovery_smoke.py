"""End-to-end discovery against the live sources, into a throwaway SQLite file.

Spec §22 requires running the system with real data after each phase, not just
passing tests. This does exactly the production path — real WebSocket, real
pump.fun enrichment, real upsert — against a temporary database so nothing is
written to Postgres.

    python scripts/run_discovery_smoke.py [seconds]

What it proves, and what it does not: it proves the providers, the normalizer
and the upsert work together on live data. It runs on SQLite, so it is *not*
verification of Postgres behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import Token
from hades.discovery.repository import TokenRepository
from hades.discovery.service import DiscoveryService
from hades.logging import configure_logging
from hades.providers.pumpfun import PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider


class FileDatabase:
    """Database-shaped, backed by a temp SQLite file."""

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
    configure_logging("INFO")
    directory = Path(tempfile.mkdtemp(prefix="hades-smoke-"))
    database = FileDatabase(directory / "smoke.db")
    await database.create_schema()

    service = DiscoveryService(
        database,  # type: ignore[arg-type]
        pumpfun=PumpFunProvider(),
        pumpportal=PumpPortalProvider(),
        poll_interval_seconds=30.0,
        poll_limit=25,
    )

    print(f"running discovery for {seconds:.0f}s against live sources")
    print(f"database: {directory / 'smoke.db'}\n")

    await service.recover()
    task = asyncio.create_task(service.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
        await task
    await service.aclose()

    async with database.session() as session:
        repository = TokenRepository(session)
        stats = await repository.stats()
        recent = (
            await session.execute(
                Token.__table__.select().order_by(Token.discovered_at.desc()).limit(8)
            )
        ).all()

    print("\n" + "=" * 88)
    print("COUNTERS (in-process)")
    for key, value in service.counters.as_dict().items():
        print(f"  {key:<22} {value}")

    print("\nDATABASE (source of truth)")
    print(f"  tokens_total           {stats.total}")
    print(f"  by_state               {stats.by_state}")
    print(f"  with_created_at        {stats.with_created_at}")
    print(f"  last_discovered_at     {stats.last_discovered_at}")
    print(f"  median_discovery_ms    {stats.median_discovery_latency_ms}")

    print("\nMOST RECENT ROWS")
    for row in recent:
        mapping = row._mapping
        print(
            f"  {str(mapping['token_address'])[:16]}...  "
            f"{mapping['symbol']!s:<10} "
            f"src={mapping['source']!s:<11} "
            f"created={mapping['created_at']} "
            f"ref={'yes' if mapping['raw_provider_reference'] else 'no'}"
        )

    await database.dispose()

    if stats.total == 0:
        print("\nNo tokens persisted. That is a failure, not a quiet market.")
        return 1
    print(f"\nOK: {stats.total} tokens persisted from live sources.")
    return 0


if __name__ == "__main__":
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    sys.exit(asyncio.run(main(window)))
