"""The migrations, run against a real PostgreSQL.

Migrations are the one part of the system that runs in production and, without
a test like this, never anywhere else. Hades V1 shipped a migration that failed
on its first real attempt — for a reason (`CONCURRENTLY` outside an autocommit
block) that no amount of reading the file would have surfaced.

Upgrade to head, then downgrade to base, then upgrade again. The round trip is
the point: a downgrade that half-works leaves a database that the next upgrade
cannot fix, and that is discovered at the worst possible moment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "hades_migration_test"

# These tests are sync: alembic's command API runs its own event loop, so they
# cannot be async. Database inspection is therefore driven through asyncio.run
# rather than a sync driver — the project has no sync driver anywhere, on
# purpose (docs/DECISIONS.md D3), and a test-only one would be a second way to
# reach the database that production never uses.


@pytest.fixture
def migration_dsn(postgres_server: object | None, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A dedicated, empty database so migrations do not fight the ORM schema."""
    if postgres_server is None:
        pytest.skip("pgserver is not installed; cannot verify migrations on real PostgreSQL")

    psql = postgres_server.psql  # type: ignore[attr-defined]
    psql(f"DROP DATABASE IF EXISTS {DB_NAME}")
    psql(f"CREATE DATABASE {DB_NAME}")

    base: str = postgres_server.get_uri()  # type: ignore[attr-defined]
    dsn = base.rsplit("/", 1)[0] + f"/{DB_NAME}"

    # migrations/env.py takes the URL from Settings, so this is how alembic is
    # pointed at the test database — the same single source of truth the app
    # uses, rather than a second one that could drift.
    monkeypatch.setenv("HADES_DATABASE_URL", dsn.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        yield dsn
    finally:
        psql(f"DROP DATABASE IF EXISTS {DB_NAME}")


def alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


def _async_dsn(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://")


def _inspect(dsn: str, work: Any) -> Any:
    async def run() -> Any:
        engine = create_async_engine(_async_dsn(dsn))
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(work)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def table_columns(dsn: str, table: str) -> set[str]:
    columns: set[str] = _inspect(
        dsn,
        lambda sync_conn: {c["name"] for c in sa.inspect(sync_conn).get_columns(table)},
    )
    return columns


def table_names(dsn: str) -> set[str]:
    names: set[str] = _inspect(dsn, lambda sync_conn: set(sa.inspect(sync_conn).get_table_names()))
    return names


def execute(dsn: str, statement: str, params: dict[str, Any] | None = None) -> None:
    async def run() -> None:
        engine = create_async_engine(_async_dsn(dsn))
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text(statement), params or {})
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_upgrade_to_head_creates_the_expected_schema(migration_dsn: str) -> None:
    command.upgrade(alembic_config(), "head")

    assert {
        "tokens",
        "market_snapshots",
        "feature_observations",
        "signals",
        "risk_decisions",
        "paper_trades",
    } <= table_names(migration_dsn)

    # Spec §11's immutable record. Note what is absent: no updated_at, and no
    # code path anywhere issues an UPDATE against it.
    assert table_columns(migration_dsn, "feature_observations") == {
        "id",
        "token_id",
        "token_address",
        "observed_at",
        "feature_version",
        "features",
        "stored_at",
    }
    assert table_columns(migration_dsn, "signals") == {
        "id",
        "observation_id",
        "token_id",
        "token_address",
        "strategy",
        "strategy_version",
        "created_at",
        "conditions",
        "stored_at",
    }

    # Every column §7 of the spec requires of a discovered token, plus the
    # on-chain reference (0002), the retry budget (0003) and tracking (0004).
    assert table_columns(migration_dsn, "tokens") == {
        "id",
        "token_address",
        "symbol",
        "name",
        "creator_address",
        "created_at",
        "discovered_at",
        "source",
        "state",
        "updated_at",
        "raw_provider_reference",
        "backfill_attempts",
        "tracking_started_at",
        "next_snapshot_at",
        "last_snapshot_at",
        "snapshot_count",
        "snapshot_failures",
    }

    # Spec §9's metric list, minus what no free source supplies, plus full
    # provenance. The raw curve reserves are the primary record; price,
    # market cap and liquidity derive from them.
    assert table_columns(migration_dsn, "market_snapshots") == {
        "id",
        "token_id",
        "token_address",
        "provider_name",
        "provider_updated_at",
        "observed_at",
        "received_at",
        "stored_at",
        "token_age_seconds",
        "tier",
        "virtual_sol_reserves",
        "virtual_token_reserves",
        "real_sol_reserves",
        "real_token_reserves",
        "total_supply",
        "base_decimals",
        "quote_decimals",
        "price_sol",
        "market_cap_sol",
        "liquidity_sol",
        "market_cap_usd",
        "sol_price_usd",
        "is_complete",
        "last_trade_at",
        "reply_count",
        "provider_data_age_seconds",
        "is_stale",
    }


def test_token_address_is_actually_unique_in_postgres(migration_dsn: str) -> None:
    """The constraint the whole idempotency guarantee rests on.

    The repository tests prove our SQL behaves; this proves the database would
    stop us even if that SQL were wrong.
    """
    command.upgrade(alembic_config(), "head")

    insert = (
        "INSERT INTO tokens (id, token_address, source, state, discovered_at, updated_at) "
        "VALUES (gen_random_uuid(), :addr, 'test', 'DISCOVERED', now(), now())"
    )
    address = {"addr": "SomeMintAddressThatIsLongEnough1234"}

    execute(migration_dsn, insert, address)
    with pytest.raises(sa.exc.IntegrityError):
        execute(migration_dsn, insert, address)


def test_token_state_enum_rejects_an_unknown_value(migration_dsn: str) -> None:
    """The native enum is a real constraint, not documentation.

    A typo'd state would otherwise sit in the column and quietly fall out of
    every state-filtered query.
    """
    command.upgrade(alembic_config(), "head")

    with pytest.raises(sa.exc.DBAPIError):
        execute(
            migration_dsn,
            "INSERT INTO tokens "
            "(id, token_address, source, state, discovered_at, updated_at) "
            "VALUES (gen_random_uuid(), 'x', 'test', 'NOT_A_STATE', now(), now())",
        )


def test_downgrade_then_upgrade_round_trips(migration_dsn: str) -> None:
    """A downgrade that half-works is discovered at the worst possible moment."""
    config = alembic_config()

    command.upgrade(config, "head")
    assert "tokens" in table_names(migration_dsn)

    command.downgrade(config, "base")
    names = table_names(migration_dsn)
    assert not names & {
        "tokens",
        "market_snapshots",
        "feature_observations",
        "signals",
        "risk_decisions",
        "paper_trades",
    }

    command.upgrade(config, "head")
    assert "raw_provider_reference" in table_columns(migration_dsn, "tokens")
    assert "market_snapshots" in table_names(migration_dsn)


def test_stepwise_upgrade_matches_a_direct_one(migration_dsn: str) -> None:
    """0001 then 0002 must land where `head` lands.

    An existing deployment upgrades one revision at a time; a fresh one goes
    straight to head. They must agree, or the two diverge silently over time.
    """
    config = alembic_config()

    command.upgrade(config, "0001")
    assert "raw_provider_reference" not in table_columns(migration_dsn, "tokens")

    command.upgrade(config, "0002")
    columns = table_columns(migration_dsn, "tokens")
    assert "raw_provider_reference" in columns
    assert "backfill_attempts" not in columns

    command.upgrade(config, "head")
    assert "backfill_attempts" in table_columns(migration_dsn, "tokens")
