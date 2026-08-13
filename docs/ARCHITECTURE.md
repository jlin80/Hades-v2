# Architecture

## Shape

A modular monolith in a single process. One FastAPI application, one PostgreSQL
database, one container image. No message broker, no cache layer, no separate
services.

```
                 SOLANA DATA SOURCES        (phase 1)
                         |
                  TOKEN DISCOVERY           (phase 1)
                         |
                   DATA COLLECTOR           (phase 2)
                         |
                  NORMALIZATION             (phase 2)
                         |
                    VALIDATION              (phase 2)
                         |
                     DATABASE               <- phase 0 ends here
                         |
                  SNAPSHOT TRACKER          (phase 2)
                         |
                   OUTCOME TRACKER          (phase 3)
```

Phase 0 implements the database layer, configuration, logging and health
reporting. Everything above it is unbuilt.

## What exists today

```
src/hades/
  clock.py          utc_now() and age_ms(). The only source of "now".
  config/           Settings, loaded from the environment via pydantic-settings.
  database/
    base.py         DeclarativeBase with a fixed constraint naming convention.
    engine.py       Async engine, session factory, connectivity probe.
  observability/
    logging.py      structlog; JSON in production, console locally.
  api/
    app.py          Application factory and lifespan.
    state.py        Engine and session factory, owned by the app, injected per request.
    routes/health.py  /health and /status.
  main.py           Entrypoint.
```

There is no `discovery/`, `market_data/`, `snapshots/` or `outcomes/` package.
Empty packages are not scaffolding, they are claims about structure that has not
been designed yet.

## Decisions and their reasons

Hades v1 was not a failure of code quality — it held `mypy --strict` clean
across 459 files with 916 tests. It failed because well-built components were
never wired to each other, and because an event bus grew into the dominant
source of complexity. The decisions below are mostly reactions to specific,
documented v1 failures.

### One process, direct calls, no event bus

v1 ran ~12 runtimes on one event loop communicating through Redis Streams. The
consumer group fell ~50,000 events behind — exactly at the stream's `MAXLEN`,
meaning events were being deleted before they were read. Derived events like
`OrderFilled` queued behind the entire discovery firehose, so a trade could
execute and take four hours to appear in PostgreSQL, or never appear at all.
A fix that split the stream into twelve lanes shipped with an `await` ordering
bug that left nine of twelve runtimes subscribed to lanes nobody read; the
resulting drop in lag was initially misread as success.

Phase 1 will call the discovery service directly and write to PostgreSQL
synchronously. A queue is introduced only when a measured throughput problem
requires one, and it will carry a specific justification.

### Everything is wired the moment it is written

The recurring v1 pattern was correct code with no caller: `Position.mark()` was
never invoked so unrealized PnL was permanently zero; `RiskFactsPort` had no
implementation so every candidate evaluated against a null object that reported
`security_approved = True`; `KnowledgeFeedback.record_outcome()` had zero callers
so the training dataset stayed single-class forever; the entire 15-strategy
Strategy Engine published events nobody subscribed to.

The rule here: a component is not finished when its unit tests pass. It is
finished when something calls it and an end-to-end test proves the call happens.
Phase 0's own chain — settings, engine, alembic, then `/status` reading the
applied revision back out of the database — is exercised end to end by
`tests/test_database_integration.py`.

### PostgreSQL is the source of truth

Nothing important lives only in memory. After a restart the system reads its
state from the database and continues. `pool_pre_ping` makes a brief database
outage survivable.

There is deliberately no self-healing watchdog. v1 ran one that called
`engine.dispose()` on the first failed probe; destroying the pool under load
produced "too many clients" cascades and roughly 52 false recoveries per 300
log lines. A failing probe is reported, not acted upon.

### Configuration exists only when its consumer does

v1 shipped a documented `SCORING_*` namespace, rendered in the dashboard's
configuration screen, wired to nothing — and colliding by name and default value
with the real `SECURITY_MIN_SECURITY_SCORE`. An operator tuning it changed
nothing, silently.

`tests/test_config.py` enforces the contract in both directions: every setting
is documented in `.env.example`, and every documented variable is consumed
either by `Settings` or by `docker-compose.yml` via an explicit allowlist.

### Reported state is measured state

`/health` runs a real `SELECT 1` and returns 503 when it fails. `/status`
reports the actual Alembic revision read from the database.

`/status` does not report `tokens_discovered`, `snapshots_collected` or a
`providers` map. Those keys arrive with the phases that make them real.
v1 had 29 API handlers that caught exceptions and returned empty results with no
logging, so during a genuine outage every dashboard panel showed zeros and
nothing distinguished "idle" from "broken".

### Trading is impossible by construction

No wallet, no signer, no private key handling, no dependency capable of building
or submitting a transaction. `tests/test_no_trading_capability.py` fails the
build if such a dependency or code path appears. This is structural, not a
configuration flag, because configuration can be flipped.

## Constraints carried into Phase 1

These come from v1's post-mortem and are commitments, not suggestions.

**One primary source, one fallback, then the database cache.** v1 integrated
Raydium, Pump.fun, DexScreener, Orca, Jupiter and Meteora. In a 24-hour sample
Pump.fun supplied 98.6% of discovered tokens (2,122 of 2,156), Raydium supplied
31, and DexScreener and Orca supplied effectively zero. Meanwhile the Jupiter
and Meteora adapters had been returning 404 for a period without being marked
unhealthy. Six integrations delivered the coverage of roughly one.

**Provider endpoints are configuration, never constants.** Jupiter retired
`quote-api.jup.ag`; the host stopped resolving and v1's honeypot check silently
rejected every candidate with zero errors logged. Every provider URL is a
settings value with an explicit health signal.

**Every provider failure is attributable.** A failure must record provider,
endpoint, error type, status code, token and retry count. No `except Exception:
pass`, anywhere, ever — enforced by ruff's `BLE` and `TRY` rules.

**HTTP clients get explicit connection limits.** v1 spent a long investigation
blaming Helius for connection churn that was its own doing: `httpx.Limits` was
never set anywhere in the codebase. Phase 1's client sets pool limits and
timeouts explicitly at construction.

**Raw data tables get a retention policy on the day they are created.** v1's
`features` table had none, grew to 618,000 rows, and a `count(distinct token_id)`
over it took 36 seconds — hit concurrently by several dashboard pollers, which
exhausted the connection pool. Index for the actual query pattern, not the
obvious column.
