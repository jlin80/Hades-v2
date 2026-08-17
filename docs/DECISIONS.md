# Architecture decisions

Each entry states the decision, the problem it solves *today*, and what was rejected.
Anything that cannot fill in "the problem it solves today" does not get built —
that is the anti-over-engineering rule from the spec, applied to this file first.

## What V1 taught, and what V2 does differently

Hades V1 is not a failed build. It is a working, tested, ~40k-line platform whose
last four months of session notes are almost entirely about *infrastructure it does
not need*: a Redis Stream consumer falling one full `MAXLEN` window behind, a cursor
pointing at trimmed entries, derived events (`OrderFilled`, `PositionOpened`) queued
behind a discovery firehose and lost to the trim, and a dashboard query so slow it
degraded the pipeline it was measuring. It executed a real paper trade on 2026-08-13
and *no row was ever written* — the trade happened somewhere the database never
heard about. `knowledge_lessons` has been at 0 since the beginning.

The lesson V2 takes is not "V1 was badly built". It is that **V1 never got to the
question it existed to answer**, because the machinery between a token and a measured
outcome had too many places to fail. Everything below follows from that.

### D1 — No message bus

**Decision:** no Redis, no Kafka, no event bus. One process, direct calls, PostgreSQL.

Every lost trade in V1 was lost in transit between components. A bus is what makes a
component's output a *message* that can queue, expire, get trimmed or be routed by
class name into a colliding key (`StrategyPromoted`, defined twice, silently
overwritten in the registry). A direct function call cannot be dropped by a broker.

**Rejected:** keeping the bus with a priority lane. That was V1's own fix and it is
correct *for V1* — but it treats "a computed result can be discarded in transit" as a
condition to mitigate rather than one to not create.

**Reversible when:** a measured bottleneck shows a single process cannot keep up.
Not before.

### D2 — No DDD / CQRS / Event Sourcing

**Decision:** a plain modular monolith. Modules, functions, a repository per table.

V1's version-drift bug is the clearest example of the cost: `pyproject` said `0.10.0`
while every HTTP surface announced `0.3.0`, because the layers made it non-obvious
that a version lived in two places. The patterns were applied competently; they still
bought indirection that a system with no proven edge cannot pay for.

**Rejected:** "clean architecture from the start, so we do not have to refactor."
Refactoring 3,000 lines is cheaper than navigating 40,000.

### D3 — Async driver enforced at startup

**Decision:** `Settings` rejects any `database_url` that is not `postgresql+asyncpg`.

V1 spent three sessions attributing worker timeouts to providers — Helius, DexScreener,
pumpfun — before measuring from inside the container and finding the providers healthy.
The shape that fits is a starved event loop or an exhausted pool. A sync driver in an
async engine produces exactly that, and it produces it *as a provider problem*. The
check costs one validator and removes one candidate cause permanently.

### D4 — `/status` reports null, never a placeholder zero

**Decision:** a counter whose phase does not exist is **absent** from the payload,
listed by name in `not_implemented`. A counter whose table exists but whose database
is unreachable is `null`.

`orders_filled: 0` on V1's dashboard did not mean "no trades". It meant a trade had
filled and nothing recorded it. A zero cannot distinguish *nothing happened* from
*we cannot see*, and only the second is a bug. Making that distinction structural is
free now and impossible to retrofit once dashboards depend on the zeros.

### D5 — The process survives a dead database

**Decision:** startup logs an unreachable database and continues; `/health` returns
200 with `status: degraded`.

A crash loop cannot serve the endpoint that explains why it is crash-looping. The
health check in `docker-compose.yml` measures the API process for the same reason —
V1 fixed this exact thing in `a261379`.

### D6 — The `tokens` table exists in Phase 0

**Decision:** one migration, one table, ahead of the Phase 2 code that will fill it.

This is the one place Phase 0 reaches forward, and it is D4's cost: `/status` must
report measured numbers, and a counter with no table behind it can only be fabricated.
`token_address` is unique, which is what will make discovery idempotent across
restarts. Nothing else is modelled.

### D7 — Paper-only is enforced by an AST scan, not a promise

**Decision:** `tests/test_safety.py` parses every file under `src/` and fails the
build if a signing library (`solana`, `solders`, `nacl`, …) is imported or a
key-material identifier appears.

V1 proved the technique works — its AST isolation test for the Research Lab held for
months, and a sibling scan caught the duplicate event-class collision. A constraint
that is only documented drifts; one that breaks CI does not.

### D8 — Pure-Python runtime, no ML extras

**Decision:** no numpy, pandas, scikit-learn, lightgbm or pyarrow, in the image or in
the default install.

The homelab CPU is an AMD G-T56N (Bobcat) with no AVX/AVX2/SSE4.2. Wheels built for a
newer baseline die with `SIGILL` on it — this took down Hermes (`@libsql/client`) and
the QuantEngine bot (`numpy2/pyarrow`). V1 avoided it by installing only `.[dev]`.
V2 makes it a property of the dependency list rather than of the Dockerfile.

Phase 17-style research runs in pure Python or it runs somewhere else.

### D9 — stdlib JSON logging, no structlog

**Decision:** ~60 lines of `logging.Formatter`.

Same reasoning as D8, plus: structlog's value is processor pipelines, and we have one
processor. `configure_logging` is idempotent because a stacked handler double-prints
every line, which reads in the logs as double the activity.

### D10 — Idempotency lives in a unique constraint, not in a "seen" set

**Decision:** `tokens.token_address` is unique, and discovery writes through a single
`INSERT ... ON CONFLICT (token_address) DO UPDATE ... WHERE` statement.

V1 deduplicated in a `seen_registry` with a 24-hour TTL. That worked, and it also meant
the process could not answer "have we seen this?" without its own memory being intact —
and a restart is precisely when the answer matters and the memory is gone. The database
survives restarts; a set does not.

The conflict branch is where the care is, and each clause is there because losing it
would be silent:

- `COALESCE(existing, incoming)` on every enrichable column. The WebSocket path has no
  `created_at`; a naive upsert would blank out the authoritative one on every
  re-delivery, and `token_age` — the input to every early-window feature — would quietly
  become uncomputable.
- `discovered_at` and `source` are **not** enrichable. First sighting, first attribution.
  A later sighting moving `discovered_at` forward would make our detection latency look
  arbitrarily good.
- `state` is not enrichable either, so a re-announced token cannot be dragged from
  TRACKING back to DISCOVERED and have its snapshot schedule restarted.
- The `WHERE` clause means the update only fires when the sighting actually adds
  something, which is what makes "enriched" and "unchanged" distinguishable instead of
  every re-observation bumping a timestamp.

**Verified against both SQLite and real PostgreSQL 16.** Every test in
`tests/test_discovery_repository.py` runs twice, and `tests/test_migrations.py` applies
the real migrations — upgrade to head, downgrade to base, upgrade again, plus stepwise —
against a live server.

That was initially recorded here as an open caveat: there is no Postgres installed on the
development machine and no Docker. It was closed rather than carried, because "the same
SQL" is a claim about two engines and asserting it from one is not evidence. `pgserver`
bundles a PostgreSQL binary and runs it from a temp directory — no Docker, nothing
installed system-wide, dev dependency only. If it is ever unavailable on some platform,
those tests **skip with a stated reason** rather than silently passing.

The migration suite immediately earned itself: adding `backfill_attempts` in D12 broke
the expected-schema assertion, which is exactly the drift it exists to catch.

### D11 — Nothing that can fail over the network sits in the write path

**Decision:** persisting a discovered token makes no HTTP call. Missing data is filled by
a separate periodic pass.

This one was not designed, it was **found by running the system.** The original code
fetched the authoritative `created_timestamp` inline for every pushed mint, and live it
failed two ways at once: 49 of 51 fetches returned 404 (the socket announces a mint
before pump.fun indexes it), and the failing call's latency was being charged to
`discovered_at`, corrupting the exact measurement it was meant to support.

The fix has two halves. `observed_at` is stamped by the provider at parse time and is
what becomes `discovered_at`, so nothing we do afterwards can inflate it. And enrichment
became `backfill_created_at()`, a pass over rows where `created_at IS NULL`, which runs
when the tokens are older and the indexing race has resolved.

Full numbers in `docs/DATA_SOURCES.md`. The measurement error turned out to be ~190 ms of
a 2.4 s reading, so the mechanism mattered more than the magnitude — but the wasted
request rate dropped from ~0.65/s of pure 404s to almost nothing, against a primary whose
rate limit is unpublished and is our single largest technical risk.

### D13 — Tracking is a bounded sample, and says so

**Decision:** a hard concurrency limit on tracked tokens, a one-hour horizon, and
`eligible_waiting` reported in `/status`.

Not a safety margin — the arithmetic leaves no choice. The primary sustains 1.64 req/s,
Pump.fun creates 0.24–0.55 tokens/s, and §8's schedule costs 434 snapshots per token per
day: tracking everything needs **64x to 146x** the available capacity. Measured live at a
capacity of 10, we tracked 10 tokens and declined 85.

Three properties follow, and each is a deliberate trade:

* **Admission is one-way.** A token keeps its slot until it ages out. Churning slots would
  sample more tokens with truncated series, and a truncated series cannot answer a question
  about the first minutes. Complete series for fewer tokens is the right side of that trade.
* **Newest first.** At capacity the younger token is strictly more valuable: the older one
  has already spent some of its early window unobserved.
* **The bias is stated, not hidden.** Admitted tokens are the ones created when a slot
  happened to be free. Close to random in time, but not a uniform sample, and research must
  not treat it as one.

`/status` publishes `snapshots_per_token` and `estimated_requests_per_second` alongside the
limit, so raising the limit is a visible decision rather than an edit to a constant. There
is a test asserting the default stays under the measured 1.64 req/s.

### D14 — Store raw curve reserves; derive everything else

**Decision:** `market_snapshots` keeps `virtual_sol_reserves`, `virtual_token_reserves`,
`real_sol_reserves`, `real_token_reserves` and `total_supply` as the primary record. Price,
market cap and liquidity are stored too, but as functions of those.

Spec §11 requires the features behind a decision to stay intact and reproducible. Storing a
provider's derived price would make that impossible, because there would be nothing to
recompute *from* — a formula error found in six months would be fatal to everything
collected before it, instead of fixable.

The formulas are verified against the provider's own figure: for a live token our derived
market cap matched pump.fun's to five decimal places. A decimals mistake is off by a factor
of a thousand and cannot agree by accident, which is what makes that a check rather than a
restatement. `test_derive.py` pins the case.

**`bonding_curve_progress` is deliberately absent.** It is in §9's list, and it is
computable — but only with curve constants that are not established for the current
pump.fun program variant. Two plausible derivations of the same token's progress disagreed
(33% by SOL raised, 65% by tokens remaining), so at least one constant is wrong. A progress
figure that is quietly wrong would poison a feature; NULL does not. The raw reserves make it
computable the moment the constants are pinned down.

### D15 — Features are pure functions, and look-ahead is impossible by signature

**Decision:** every feature is a pure function of a snapshot series.
`compute_features(series, as_of=...)` truncates to `as_of` before anything runs, so no
feature can see a later observation.

Temporal leakage is the one defect that **fails looking like success** — excellent offline
metrics from a model that cannot work. Hades V1 built its whole knowledge loop so the
leaking version was impossible to write, and this is the equivalent here: it is a property
of the call signature, not a rule to remember. A test inserts a violent price move after
the decision point and asserts the vector is unchanged.

Purity also buys reproducibility, which §11 requires: any number in the dataset can be
recomputed from stored snapshots and checked by hand, with no database and no clock.

`FEATURE_VERSION` is stamped on every vector. A definition change must bump it, because a
stored vector's meaning is fixed by the version that produced it — one whose formulas
silently changed underneath it is not intact, it only looks intact.

**Missing is None, never 0.0.** A zero velocity claims the price did not move; not knowing
whether it moved is a different fact. Collapsing them lets unmeasured periods masquerade as
calm ones.

And every rate divides by **observed** elapsed time, never the configured interval. Phase 3
measured those at ~12.2s versus 10s — a consistent 20% error in a direction nobody would
notice.

### D12 — The backfill retry budget is a column, not a counter

**Decision:** `tokens.backfill_attempts` is persisted and incremented in the database;
tokens past `discovery_backfill_max_attempts` drop out of the queue and are reported in
`/status` as `tokens_backfill_exhausted`.

Some mints reach the WebSocket but never appear in pump.fun's index at all — measured,
404 on every attempt minutes apart, most likely external-launchpad tokens. Without a
budget, those sit at the head of a queue ordered by oldest-sighting-first and **starve
the tokens whose indexing race merely has not resolved yet**. The failure is not that we
waste requests; it is that the tokens we *could* have fixed never get looked at.

Persisted rather than in-memory for the same reason as D10: a restart must not hand a
hopeless token a fresh budget, or the starvation returns on the next deploy.

A rate limit does **not** spend a token's budget. Being throttled is our problem, not the
token's, and charging it would burn every budget during an outage and permanently drop
tokens that were fine.

Exhausted tokens keep `state = DISCOVERED`. Marking them INACTIVE was considered and
rejected: it would conflate "we could not get metadata" with "this token is dead", and
those are different facts about different things. What they lose is `created_at`, hence
`token_age`, hence every early-window feature — so they cannot be tracked, and the count
is surfaced because it is a real loss of universe coverage.
