# Known issues

Real problems and accepted technical debt. Nothing is hidden here; an empty
section would be a warning sign, not a good sign.

Format per task.md §21: Problem, Impact, Root cause, Current status, Next action.

---

## 1. The fallback provider is not equivalent to the primary

**Problem.** DexScreener's `/token-profiles/latest/v1` yields a token address
and nothing else. When the primary is down, discovery continues but stores rows
with NULL symbol, name and `first_seen_at`.

**Impact.** Tokens discovered during a GeckoTerminal outage carry no age
information, and `first_seen_at` is the field the research phases need most —
token age at detection is a core feature.

**Root cause.** No second free endpoint was found that supplies both new-token
discovery and pool creation time. `/latest/dex/search` was rejected for this
role: probing showed it is a cross-chain text search whose first result was a
Base pool, not a Solana discovery feed.

**Current status.** Accepted, and visible rather than hidden — the provider that
discovered each token is stored in `discovery_provider`, so rows collected
during a fallback window can be identified and, if needed, backfilled.

**Next action.** In Phase 2, when market snapshots start calling GeckoTerminal
per token anyway, backfill `first_seen_at` for rows discovered by the fallback.

---

## 1b. Rejected tokens are logged but not stored

**Problem.** Validation rejections are emitted as structured log events with a
machine-readable reason, but nothing persists them.

**Impact.** task.md §18 Phase 4 asks "how many records were rejected?". That
question currently requires log aggregation rather than a query, and log
retention is 10 MB × 3 files per container.

**Root cause.** Deliberate. A `discovery_rejections` table is Phase 4's job, and
creating it now would be building ahead of the phase.

**Current status.** Accepted for Phase 1. Every rejection carries its reason as
a `RejectionReason` enum value, so the data model for that table already exists
and is exercised.

**Next action.** Add the table in Phase 4 alongside the report that reads it,
with a retention policy from day one.

---

## 2. The baseline migration is empty

**Problem.** `0001_baseline` creates no tables, so schema migration logic is
unproven beyond the creation of `alembic_version`.

**Impact.** Low now, but the first real migration in Phase 1 will exercise
autogenerate, constraints and downgrade paths for the first time.

**Root cause.** Intentional. Phase 0 has no legitimate table, and inventing a
placeholder to make the schema look non-empty would be building ahead.

**Current status.** Accepted. The migration chain, the async Alembic runner and
the `/status` readback are all genuinely exercised end to end.

**Next action.** When the Phase 1 `tokens` table lands, test `upgrade` and
`downgrade` against a real database, and verify the unique constraint that
enforces discovery idempotency.

---

## 3. Compose cannot be validated on the workstation

**Problem.** Docker is installed on neither Windows nor WSL, so
`docker compose up` can only be run on CT 204.

**Impact.** Slower feedback loop. A Dockerfile or compose error is found on the
container, not locally.

**Root cause.** Environment choice: the workstation has Python 3.12 natively and
the deployment target is a Proxmox container.

**Current status.** Accepted for now. Lint, type checking and unit tests all run
locally and catch most problems before deployment.

**Next action.** Revisit if the loop becomes painful. Installing Docker Engine
inside the existing WSL2 Ubuntu is the cheapest fix.

---

## 4. The repository lives inside a OneDrive-synced folder

**Problem.** `.git` sits under `C:\Users\jinlh\OneDrive\...`, so OneDrive syncs
git internals while git is writing them.

**Impact.** Risk of index corruption or sync conflicts, particularly during
rebases or long operations.

**Root cause.** Deliberate choice to keep the existing project location.

**Current status.** Mitigated but not eliminated. `.gitignore` excludes `.venv`,
caches and build output, so the synced volume stays small.

**Next action.** If a `.git` conflict ever appears, move the repository out of
OneDrive immediately. GitHub is the durable copy, not OneDrive.

---

## 5. httpx2 is pinned by a Starlette deprecation

**Problem.** Starlette 1.6 deprecates `httpx` in favour of `httpx2` for its
TestClient. With warnings-as-errors, importing TestClient with the old httpx
fails the suite outright, so `httpx2` is a hard dev dependency.

**Impact.** Development only; no runtime effect. `httpx2` is a recent package
and the ecosystem is mid-migration.

**Root cause.** Upstream transition, discovered because the warnings-as-errors
gate did its job on the very first test run.

**Current status.** Accepted and pinned to `>=2.10,<3.0`.

**Next action.** When Phase 1 adds a production HTTP client, decide deliberately
between httpx and httpx2 for runtime, and set explicit `Limits` and timeouts on
it either way (see issue 6).

---

## 6. Carried-over v1 anti-patterns not yet applicable

**Problem.** Three v1 failures have no code to attach to yet, so nothing
currently enforces them.

**Impact.** They are the most likely ways Phase 1 and Phase 2 could repeat v1's
history.

**Root cause.** Phase 0 has no HTTP client and no data tables.

**Current status.** Documented as commitments in
[ARCHITECTURE.md](ARCHITECTURE.md):
- HTTP clients must set explicit `httpx.Limits`. v1 never set them anywhere and
  spent a long investigation blaming its RPC provider for its own connection
  churn.
- Raw data tables need a retention policy from day one. v1's `features` table
  had none, reached 618,000 rows, and a `count(distinct token_id)` over it took
  36 seconds while several pollers ran it concurrently.
- Indexes must match the real query pattern. A single-column index on
  `computed_at` could not serve a `(token_id, computed_at)` query.

**Next action.** Convert each into an executable check when the corresponding
code exists, the way `test_no_trading_capability.py` enforces §16 today.

---

## Resolved

**Docker chose the `vfs` storage driver.** Docker inside an unprivileged LXC on
ZFS falls back to `vfs`, which copies every image layer instead of sharing it —
this filled the v1 container's disk and shut PostgreSQL down.
`scripts/provision-ct.sh` writes `/etc/docker/daemon.json` before installing
Docker and asserts the driver afterwards. Verified on CT 204: `overlay2`.

**Locale warnings during provisioning.** The Debian 13 template ships without a
generated locale, so every apt operation emitted perl warnings. Fixed by
generating `en_US.UTF-8` in the container. Cosmetic, but noise hides signal.
