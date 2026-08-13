# Hades V2

A data collection platform for Solana memecoins.

**Phase 0 — project foundation.** The system currently starts, connects to
PostgreSQL, applies migrations and reports its real state over HTTP. It does not
yet discover tokens, collect market data, or track outcomes.

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

A healthy response reports a live database round-trip and the applied migration
revision:

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "environment": "development",
  "phase": 0,
  "uptime_seconds": 12.4,
  "database": {
    "connected": true,
    "latency_ms": 1.31,
    "migration_revision": "0001",
    "error": null
  },
  "pending_capabilities": [
    "token_discovery (phase 1)",
    "market_snapshots (phase 2)",
    "outcome_tracking (phase 3)",
    "data_validation_report (phase 4)"
  ]
}
```

`/status` deliberately does not report `tokens_discovered`, `snapshots_collected`
or a `providers` map. Those components do not exist yet, and reporting zeros for
them would be fabricated telemetry.

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
  database/       async engine, session factory, connectivity probes
  observability/  structured logging
  clock.py        the single source of "now", always UTC
migrations/       alembic revisions
tests/            unit tests, plus integration tests behind a marker
docker/           Dockerfile
docs/             architecture, operations, known issues
```

Directories for `discovery/`, `market_data/`, `snapshots/` and `outcomes/` are
absent on purpose. They are created by the phase that implements them.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — structure and the reasoning behind it
- [docs/OPERATING.md](docs/OPERATING.md) — running, deploying and debugging
- [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) — open problems and technical debt
- [CHANGELOG.md](CHANGELOG.md) — what changed and when
