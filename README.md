# Hades V2

A data collection platform for Solana memecoins.

**Phase 1 — token discovery.** The system discovers new Solana tokens from a
live feed, validates them, and stores them without duplicates — currently
detecting tokens 80-90 seconds after their pool is created. It does not yet
collect market data or track outcomes.

It buys nothing and sells nothing, and is structurally incapable of doing so:
no wallet, no signer, no private keys, no transaction-submitting dependency.
That constraint is enforced by the test suite, not by configuration
(`tests/test_no_trading_capability.py`).

## Why this exists

The goal is to answer one question before any trading logic is written:

> Can we reliably detect relevant new Solana tokens and store complete
> historical snapshots of their behaviour?

Only once a real dataset exists does quantitative research begin. The
development sequence is fixed and stages are not skipped:

```
data collection -> validation -> feature snapshots -> outcome tracking
   -> dataset -> research -> hypothesis validation -> backtest
   -> paper trading -> live trading
```

## Quick start

```bash
cp .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD` to something real, then:

```bash
docker compose up -d --build
```

Compose starts PostgreSQL, waits for it to become healthy, runs
`alembic upgrade head` to completion, and only then starts the API.

Check that it is actually working:

```bash
curl -s localhost:8000/status | python -m json.tool
```

A healthy response reports a live database round-trip, the applied migration
revision, and measured discovery state:

```json
{
  "status": "healthy",
  "version": "0.2.0",
  "phase": 1,
  "database": {
    "connected": true,
    "latency_ms": 10.8,
    "migration_revision": "0002",
    "error": null
  },
  "discovery": {
    "enabled": true,
    "scheduler_running": true,
    "interval_seconds": 30.0,
    "tokens_discovered": 146,
    "last_discovery_at": "2026-08-13T20:51:32.779836Z",
    "last_run": {
      "provider": "geckoterminal",
      "fetched": 20, "valid": 20, "rejected": 0,
      "inserted": 0, "duplicates": 20, "error": null
    },
    "providers": {
      "geckoterminal": {"status": "healthy", "consecutive_failures": 0},
      "dexscreener": {"status": "unknown", "consecutive_failures": 0}
    }
  },
  "pending_capabilities": [
    "market_snapshots (phase 2)",
    "outcome_tracking (phase 3)",
    "data_validation_report (phase 4)"
  ]
}
```

Two details worth reading carefully, because both are deliberate:

`dexscreener` reports `unknown`, not `healthy`. The fallback only runs when the
primary fails, so it has genuinely never been attempted — and a provider is
never marked healthy without checking it.

`tokens_discovered` is `null`, never `0`, when the database is unreachable. Zero
would mean "no tokens found"; null means "we could not look". `/status` still
reports no `snapshots_collected`, because nothing collects them yet.

## Data sources

One primary, one fallback, then the database — chosen by probing candidates live
rather than by assumption (`scripts/probe-providers.sh`).

| Role | Source | Supplies |
|---|---|---|
| Primary | GeckoTerminal `new_pools` | Address, symbol, pool address, pool creation time |
| Fallback | DexScreener `token-profiles` | Address only; symbol and pool age stay NULL |

The fallback exists so discovery continues during a primary outage. It is not a
claim that both sources see the same tokens.

## Endpoints

| Endpoint  | Purpose                                                          |
|-----------|------------------------------------------------------------------|
| `/health` | Liveness and readiness. Runs a real `SELECT 1`. HTTP 503 when the database is unreachable. |
| `/status` | Operational detail: uptime, database latency, migration revision, unimplemented capabilities. |
| `/docs`   | OpenAPI documentation.                                            |

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Quality gate — all four must pass:

```bash
ruff check .
ruff format --check .
mypy src
pytest
```

Integration tests need a live database and are skipped without one:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://hades:PASSWORD@127.0.0.1:5432/hades pytest -m integration
```

## Layout

```
src/hades/
  api/            FastAPI application, routes, request-scoped state
  config/         settings loaded from the environment
  database/       async engine, session factory, ORM models
  discovery/      providers, validation, persistence, service, scheduler
  observability/  structured logging
  clock.py        the single source of "now", always UTC
migrations/       alembic revisions
tests/            unit tests, plus integration tests behind a marker
scripts/          provisioning, verification, provider probing
docker/           Dockerfile
docs/             architecture, operations, known issues
```

Directories for `market_data/`, `snapshots/` and `outcomes/` are absent on
purpose. They are created by the phase that implements them.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — structure and the reasoning behind it
- [docs/OPERATING.md](docs/OPERATING.md) — running, deploying and debugging
- [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) — open problems and technical debt
- [CHANGELOG.md](CHANGELOG.md) — what changed and when
