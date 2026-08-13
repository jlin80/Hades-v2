# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is phase-driven: the minor version tracks the completed phase.

## [0.2.0] — 2026-08-13

Phase 1 — token discovery.

### Provider selection

Candidate endpoints were probed live before any adapter was written
(`scripts/probe-providers.sh`), because task.md §20 forbids assuming how an API
responds. The probe found that **two of the sources Hades v1 depended on are
dead**: Pump.fun's `frontend-api` returns Cloudflare 530, and both Jupiter token
endpoints are gone (404, and a host that no longer resolves). Pump.fun had
supplied 98.6% of v1's discovered tokens.

- **Primary: GeckoTerminal** `/networks/solana/new_pools` — 20 pools in 0.50s,
  carrying pool creation time, price, FDV, price changes and per-window
  buys/sells/buyers/sellers. Free, no API key.
- **Fallback: DexScreener** `/token-profiles/latest/v1` — 0.23s, Solana entries
  filtered from a cross-chain feed. Deliberately lower fidelity: it yields an
  address only, so symbol, name and pool age are stored as NULL rather than
  guessed. It exists so discovery continues when the primary is down, not as a
  claim that both sources see the same tokens.

### Added

- `tokens` table (migration `0002`) with a unique constraint on
  `token_address`. Discovery inserts with `ON CONFLICT DO NOTHING`, so repeated
  processing cannot create duplicate rows — enforced by the database, not by a
  check-then-insert race.
- Discovery service running ONE PRIMARY -> ONE FALLBACK -> DATABASE. The
  fallback is tried only when the primary fails.
- Per-provider health that starts as `unknown` and only becomes `healthy` after
  an attempt actually succeeded, with consecutive-failure counting.
- Explicit validation rejecting empty and non-base58 addresses, oversized
  symbols and names, naive timestamps and pool times in the future. Every
  rejection carries a machine-readable reason for the Phase 4 report.
- Bounded retry honouring `Retry-After`, retrying only 429 and transient 5xx.
  A 404 is never retried. Every failure is attributable: provider, endpoint,
  error type, status code, retry count.
- Explicit `httpx.Limits` and timeouts on the shared client.
- Background scheduler on a fixed interval that survives provider outages,
  database blips and unanticipated bugs without dying.
- `/status` now reports real `tokens_discovered` and `last_discovery_at` read
  from the database (so they survive a restart), scheduler state, per-provider
  health, and the outcome of the last run.
- 26 further tests, including parsers driven by responses captured from the
  live endpoints rather than invented shapes.

### Changed

- `tokens_discovered` and `last_discovery_at` moved out of
  `pending_capabilities` now that real components produce them.
- A count that cannot be measured is reported as `null`, never `0`. Zero would
  mean "no tokens found"; null means "we could not look".

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
- `scripts/bootstrap-env.sh`, generating a `.env` with a random database
  password and refusing to overwrite an existing one.
- `scripts/verify-deployment.sh`, checking a running stack against reality:
  HTTP contract, `alembic_version` agreement between the database and `/status`,
  database timezone, JSON log parseability, absence of the password from logs,
  and the Docker storage driver.
- `scripts/run-tests-against-stack.sh`, running the full suite against the
  deployment's real PostgreSQL from a throwaway container.
- 28 tests: configuration contract, UTC handling, logging, health endpoints,
  live-database integration, and a structural guard proving the project cannot
  execute a financial operation.
- Documentation: README, ARCHITECTURE, OPERATING, KNOWN_ISSUES.

### Verified

Deployed to Proxmox CT 204 (`192.168.100.44`) and validated against reality, not
assumption:

- `docker compose up -d --build` brings the stack up with the intended gating:
  PostgreSQL healthy, then `migrate` run to completion, then `api`.
- `/status` reports a live database round-trip and revision `0001`, matching the
  `alembic_version` row read directly from PostgreSQL.
- All 28 tests pass against the deployment's real database, integration tests
  included.
- The container survives a full host reboot: both services return automatically
  and the database volume, its data and the applied revision persist.
- Docker uses `overlay2`; the stack occupies 1.2 GB of 32 GB.

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
