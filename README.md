# Hades V2

Quantitative research system for **Pump.fun tokens on Solana**. Paper trading only.

The goal is not a complex bot. It is a system that can answer, with real data:

> *What observable conditions in the first minutes of a Pump.fun token have predictive
> value and produce a positive expectancy after slippage, fees and risk?*

This is a **greenfield rebuild**. Hades V1 (`../Hades`) is reference material for
mistakes and reusable decisions — not a codebase to port. See `docs/DECISIONS.md`.

## Status

**All seven phases complete.** Each one passed its gate before the next started.

The system now produces a falsifiable number end to end. What that number currently says
is in [`docs/RESEARCH.md`](docs/RESEARCH.md), and it is not flattering to the hypothesis.

Discovery and tracking are both **off by default** (`HADES_DISCOVERY_ENABLED`,
`HADES_TRACKING_ENABLED`): starting the API must never begin writing to the database as a
side effect.

| Phase | Scope | State |
|---|---|---|
| 0 | Repo, config, PostgreSQL, Alembic, Docker, logging, FastAPI, health/status, tests | ✅ |
| 1 | Verify real data sources → [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | ✅ |
| 2 | Token discovery — discover, validate, persist, deduplicate, recover | ✅ |
| 3 | Adaptive snapshot tracking, persistence, validation, stale detection | ✅ |
| 4 | Feature engine — [`docs/FEATURES.md`](docs/FEATURES.md) | ✅ |
| 5 | Signal research — one hypothesis → [`docs/SIGNALS.md`](docs/SIGNALS.md) | ✅ |
| 6 | Paper trading — risk, fills, fees, slippage → [`docs/PAPER_TRADING.md`](docs/PAPER_TRADING.md) | ✅ |
| 7 | Outcome + analytics → [`docs/RESEARCH.md`](docs/RESEARCH.md) | ✅ |

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
are applied to a live server — upgrade, downgrade, upgrade, and stepwise. `pgserver`
bundles the binary, so this needs no Docker and installs nothing system-wide.

## Data sources

Primary **pump.fun `frontend-api-v3`** (data from t=0, bonding-curve reserves), discovery
via **PumpPortal WebSocket** (push, ~0.24 creations/s measured), fallback **DexScreener**
(fast and documented, but blind for the first ~2 minutes and supplies no liquidity field).

All of that was measured, not assumed — rerun the evidence with:

```bash
.venv/Scripts/python scripts/probe_data_sources.py
.venv/Scripts/python scripts/probe_pumpportal_ws.py
```

Four metrics have no usable free source and stay NULL: `unique_buyers`,
`unique_sellers`, `buy_volume`, `sell_volume`. That constrains the Phase 5 hypothesis —
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
and liquidity from them — verified against the provider's own figure to five decimals.

**Tracking the whole universe is impossible by ~100x.** The primary sustains 1.64 req/s;
Pump.fun creates 0.24–0.55 tokens/s; the schedule costs 434 snapshots per token per day.
So the system tracks a bounded sample — 40 concurrent tokens over a one-hour horizon,
about 1.2 req/s — and `/status` reports `eligible_waiting`, the sample it is declining to
take. See [`docs/DECISIONS.md`](docs/DECISIONS.md) D13.

```bash
.venv/Scripts/python scripts/run_tracking_smoke.py 150
```

## Features

41 features, all pure functions of a snapshot series — no I/O, no clock, so every number
in the dataset can be recomputed and checked. Look-ahead is impossible by signature:
`compute_features(series, as_of=...)` truncates before anything runs.

Missing inputs give `None`, never `0.0`, and every rate divides by **observed** elapsed
time rather than the configured interval (measured: ~12.2s versus 10s).

The features §10 asks for that need per-trade data are absent, not faked — see
[`docs/FEATURES.md`](docs/FEATURES.md) for the full set, the substitutes, and what that
means for Phase 5's hypothesis.

```bash
.venv/Scripts/python scripts/run_features_demo.py 120
```

## Signals

One hypothesis — EARLY MOMENTUM — evaluated over tracked tokens, producing **research
signals and never an order**. Nothing here claims it works; every threshold is a plausible
starting point, not a value derived from evidence.

Every evaluation writes an immutable feature vector (spec §11), and only some produce a
signal. That asymmetry is the point: a signal count means nothing without the number of
chances there were to fire.

Measured over 200 seconds live: **211 observations, 5 signals (2.4%)**, and the per-clause
breakdown says something the system did not know before — 85% of observations were of
tokens holding under 1 SOL, and 72% were of tokens whose curve had not moved. Most Pump.fun
tokens are dead on arrival. See [`docs/SIGNALS.md`](docs/SIGNALS.md).

```bash
.venv/Scripts/python scripts/run_signals_smoke.py 200
```

## Paper trading

Risk engine, position management, realistic fills, TP/SL. **Simulated only** — no signer,
no wallet, no RPC.

Slippage is **computed, not assumed**: a Pump.fun token trades against a constant-product
curve whose reserves we store, so the fill price for an order size is exact. A hardcoded
slippage percentage is a free parameter that quietly decides whether a backtest shows an
edge; this one is derived from the same data the features come from.

All eight gates of §13, plus a ninth that measurement forced: Phase 5 produced four
signals on one token inside a minute, so `max_open_per_token` is 1. The engine is
fail-closed, and treats unknown as refusal.

The most consequential assumption is written down rather than buried — stop loss is
checked before take profit, because between two snapshots the price may have visited both
and we cannot see which came first. See [`docs/PAPER_TRADING.md`](docs/PAPER_TRADING.md)
for that and the four ways the simulation is optimistic.

## Outcomes and research

Triple-barrier labelling, MFE/MAE, returns at five horizons, and the §16 dataset — for
**every** observation, not just the ones a signal fired on, because the rest are the
counterfactual.

`UNRESOLVED` is kept distinct from `TIMEOUT`: "we do not know yet" and "neither barrier
was touched" are different claims.

**The finding that matters most is a bias.** A label goes final either by touching a
barrier or by its window elapsing, so early in a run the finalised set is almost entirely
tokens that moved sharply. Measured: 34 of 646 observations had a final label and *not one*
was a timeout. The report prints that warning itself rather than a number that looks like
evidence.

```bash
.venv/Scripts/python scripts/run_full_pipeline.py 300 --keep run.db
.venv/Scripts/python scripts/research_report.py run.db tp20_sl10_15m --csv dataset.csv
```

## Safety

No private keys, no signer, no transaction submission — enforced by an AST scan in
`tests/test_safety.py` that fails the build if a signing library or key-material
identifier reaches `src/`. See `docs/SAFETY.md`.

## Layout

```
src/hades/
  config.py       Settings (env-driven, frozen, rejects sync DB drivers)
  logging.py      stdlib JSON-lines logging
  db/             engine + session lifecycle, ORM models
  providers/      pump.fun (primary), PumpPortal (discovery), shared HTTP policy
  discovery/      Phase 2 — idempotent token discovery
  tracking/       Phase 3 — adaptive snapshots, curve derivations
  features/       Phase 4 — pure feature functions
  signals/        Phase 5 — the one hypothesis, immutable observations
  risk/           Phase 6 — the nine gates, fail-closed
  paper/          Phase 6 — exact curve fills, exit rules, trade lifecycle
  outcomes/       Phase 7 — triple-barrier labelling, dataset export
  research/       Phase 7 — the §17 questions
  api/            FastAPI app, /health, /status
migrations/       Alembic
scripts/          probes and smoke runs against live sources (reproducible evidence)
docs/             DATA_SOURCES.md, FEATURES.md, SIGNALS.md, PAPER_TRADING.md,
                  RESEARCH.md, DECISIONS.md, SAFETY.md, DEPLOYMENT.md
tests/
```
