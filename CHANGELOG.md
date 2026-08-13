# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is phase-driven: the minor version tracks the completed phase.

## [0.1.0] — 2026-08-13

Phase 0 — project foundation.

### Added

- FastAPI application with a lifespan that owns the database engine and session
  factory.
- `GET /health`: liveness and readiness backed by a real `SELECT 1`, returning
  HTTP 503 when the database is unreachable.
- `GET /status`: uptime, database latency, applied Alembic revision, and an
  explicit list of capabilities that do not exist yet.
- Settings loaded from the environment via pydantic-settings, rejecting a
  non-asyncpg DSN at startup and redacting the password for logging.
- Async SQLAlchemy 2.0 engine with `pool_pre_ping`, plus connectivity and
  migration-revision probes.
- Structured logging with structlog: JSON output, ISO-8601 UTC timestamps, and
  uvicorn/sqlalchemy/alembic routed through the same pipeline.
- `hades.clock` as the single source of "now", always timezone-aware UTC.
- Alembic configured for async operation, reading its DSN from application
  settings so the runner and the app cannot disagree.
- Baseline migration `0001`, deliberately creating no tables.
- Docker image: multi-stage, non-root uid 1000, no build toolchain at runtime.
- Compose stack: PostgreSQL 17.2, a one-shot `migrate` service gated on database
  health, and the API gated on migration success.
- `scripts/provision-ct.sh`, which asserts Docker uses `overlay2` and fails
  loudly on `vfs`.
- 28 tests: configuration contract, UTC handling, logging, health endpoints,
  live-database integration, and a structural guard proving the project cannot
  execute a financial operation.
- Documentation: README, ARCHITECTURE, OPERATING, KNOWN_ISSUES.

### Deliberately not added

No token discovery, market data, snapshots or outcome tracking. No Redis, no
message broker, no Prometheus or Grafana. No wallet, signer, private key
handling or any dependency capable of submitting a transaction — enforced by
`tests/test_no_trading_capability.py`.

No `discovery/`, `market_data/`, `snapshots/` or `outcomes/` packages: empty
directories are claims about structure that has not been designed yet.

No provider toggles or interval settings in configuration. Hades v1 shipped a
documented `SCORING_*` namespace wired to nothing, visible in its dashboard,
silently ignoring operator changes. `tests/test_config.py` now fails the build
if a documented variable has no consumer.
