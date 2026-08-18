# Hades V2

Quantitative research system for **Pump.fun tokens on Solana**. Paper trading only.

The goal is not a complex bot. It is a system that can answer, with real data:

> *What observable conditions in the first minutes of a Pump.fun token have predictive
> value and produce a positive expectancy after slippage, fees and risk?*

This is a **greenfield rebuild**. Hades V1 (`../Hades`) is reference material for
mistakes and reusable decisions â€” not a codebase to port. See `docs/DECISIONS.md`.

## Status

**Phases 0â€“5 complete.** Phases advance only when the current one passes its gate;
nothing beyond Phase 5 is built.

Discovery and tracking are both **off by default** (`HADES_DISCOVERY_ENABLED`,
`HADES_TRACKING_ENABLED`): starting the API must never begin writing to the database as a
side effect.

| Phase | Scope | State |
|---|---|---|
| 0 | Repo, config, PostgreSQL, Alembic, Docker, logging, FastAPI, health/status, tests | âœ… |
| 1 | Verify real data sources â†’ [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | âœ… |
| 2 | Token discovery â€” discover, validate, persist, deduplicate, recover | âœ… |
| 3 | Adaptive snapshot tracking, persistence, validation, stale detection | âœ… |
| 4 | Feature engine â€” [`docs/FEATURES.md`](docs/FEATURES.md) | âœ… |
| 5 | Signal research â€” one hypothesis â†’ [`docs/SIGNALS.md`](docs/SIGNALS.md) | âœ… |
| 6 | Paper trading | â¬œ next |
| 7 | Outcome + analytics | â¬œ |

## Run it

```bash
cp .env.example .env   # set POSTGRES_PASSWORD and HADES_DATABASE_URL
docker compose up -d   # migrate runs to head, then the API starts
curl -s localhost:8000/status
```

Locally, without Docker:

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m hades
```

## Quality gate

Every phase ends here. It must be green before the next one starts.

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check . && .venv/Scripts/mypy && .venv/Scripts/pytest
```

The storage tests run against **both SQLite and a real PostgreSQL 16**, and the migrations
are applied to a live server â€” upgrade, downgrade, upgrade, and stepwise. `pgserver`
bundles the binary, so this needs no Docker and installs nothing system-wide.

## Data sources

Primary **pump.fun `frontend-api-v3`** (data from t=0, bonding-curve reserves), discovery
via **PumpPortal WebSocket** (push, ~0.24 creations/s measured), fallback **DexScreener**
(fast and documented, but blind for the first ~2 minutes and supplies no liquidity field).

All of that was measured, not assumed â€” rerun the evidence with:

```bash
.venv/Scripts/python scripts/probe_data_sources.py
.venv/Scripts/python scripts/probe_pumpportal_ws.py
```

Four metrics have no usable free source and stay NULL: `unique_buyers`,
`unique_sellers`, `buy_volume`, `sell_volume`. That constrains the Phase 5 hypothesis â€”
see the open decision at the end of [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).

## Discovery

Two sources, one idempotent funnel. The WebSocket pushes a mint on creation; polling the
primary backfills what a disconnect lost and fills in the `created_at` the socket does not
carry. Both write through the same `ON CONFLICT` upsert, so overlap is free and neither
source has to be reliable on its own.

Measured end to end against live sources: **80 tokens persisted in 100 s**, median
discovery latency **2404 ms**. Reproduce with:

```bash
.venv/Scripts/python scripts/run_discovery_smoke.py 100
```

That writes to a throwaway SQLite file, never to Postgres.

## Tracking

Snapshots on an age-adaptive schedule (10s for the first 5 min, then 30s, then 60s),
storing raw bonding-curve reserves as the primary record and deriving price, market cap
and liquidity from them â€” verified against the provider's own figure to five decimals.

**Tracking the whole universe is impossible by ~100x.** The primary sustains 1.64 req/s;
Pump.fun creates 0.24â€“0.55 tokens/s; the schedule costs 434 snapshots per token per day.
So the system tracks a bounded sample â€” 40 concurrent tokens over a one-hour horizon,
about 1.2 req/s â€” and `/status` reports `eligible_waiting`, the sample it is declining to
take. See [`docs/DECISIONS.md`](docs/DECISIONS.md) D13.

```bash
.venv/Scripts/python scripts/run_tracking_smoke.py 150
```

## Features

41 features, all pure functions of a snapshot series â€” no I/O, no clock, so every number
in the dataset can be recomputed and checked. Look-ahead is impossible by signature:
`compute_features(series, as_of=...)` truncates before anything runs.

Missing inputs give `None`, never `0.0`, and every rate divides by **observed** elapsed
time rather than the configured interval (measured: ~12.2s versus 10s).

The features Â§10 asks for that need per-trade data are absent, not faked â€” see
[`docs/FEATURES.md`](docs/FEATURES.md) for the full set, the substitutes, and what that
means for Phase 5's hypothesis.

```bash
.venv/Scripts/python scripts/run_features_demo.py 120
```

## Signals

One hypothesis â€” EARLY MOMENTUM â€” evaluated over tracked tokens, producing **research
signals and never an order**. Nothing here claims it works; every threshold is a plausible
starting point, not a value derived from evidence.

Every evaluation writes an immutable feature vector (spec Â§11), and only some produce a
signal. That asymmetry is the point: a signal count means nothing without the number of
chances there were to fire.

Measured over 200 seconds live: **211 observations, 5 signals (2.4%)**, and the per-clause
breakdown says something the system did not know before â€” 85% of observations were of
tokens holding under 1 SOL, and 72% were of tokens whose curve had not moved. Most Pump.fun
tokens are dead on arrival. See [`docs/SIGNALS.md`](docs/SIGNALS.md).

```bash
.venv/Scripts/python scripts/run_signals_smoke.py 200
```

## Safety

No private keys, no signer, no transaction submission â€” enforced by an AST scan in
`tests/test_safety.py` that fails the build if a signing library or key-material
identifier reaches `src/`. See `docs/SAFETY.md`.

## Layout

```
src/hades/
  config.py       Settings (env-driven, frozen, rejects sync DB drivers)
  logging.py      stdlib JSON-lines logging
  db/             engine + session lifecycle, ORM models
  providers/      pump.fun (primary), PumpPortal (discovery), shared HTTP policy
  discovery/      Phase 2 â€” idempotent token discovery
  tracking/       Phase 3 â€” adaptive snapshots, curve derivations
  features/       Phase 4 â€” pure feature functions
  signals/        Phase 5 â€” the one hypothesis, immutable observations
  api/            FastAPI app, /health, /status
migrations/       Alembic
scripts/          probes and smoke runs against live sources (reproducible evidence)
docs/             DATA_SOURCES.md, FEATURES.md, SIGNALS.md, DECISIONS.md, SAFETY.md,
                  DEPLOYMENT.md
tests/
```

