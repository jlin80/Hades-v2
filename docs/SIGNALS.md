# Signals — Phase 5

Strategy `early_momentum` v1.0.0 · one hypothesis · **research signals only, no orders**

## ⚠️ Nothing here claims this works

Spec §12 requires the hypothesis to be configurable and **not presented as truth**. Every
threshold is a plausible starting point, not a value derived from evidence. There is no
evidence yet. Producing it is Phase 7's job, and the answer may be that this hypothesis
has no edge at all.

A signal count is not a result. `/status` reports `signals_total` next to
`observations_total` and carries a disclaimer for exactly that reason.

## What §12 asks for versus what is computable

§12 sketches: *token young + buying activity accelerating + volume increasing + sell
pressure below threshold + liquidity above minimum.*

Three of those five need per-trade data no free source supplies. The substitution is
stated rather than made quietly:

| §12 clause | Used instead | What is lost |
|---|---|---|
| buying activity accelerating | `market_cap_acceleration > 0` | acceleration of price, not of buyer count |
| volume increasing | `liquidity_velocity > 0` | it is **net** flow; gross buys and sells are inseparable |
| sell pressure below threshold | net flow positive at all | selling merely outweighed by buying is invisible |
| token young | `token_age_seconds` in range | nothing |
| liquidity above minimum | `liquidity_sol >= min` | nothing |

**The third row is the weakest link and worth naming plainly:** a token with heavy buying
*and* heavy selling looks identical to one with light buying and no selling, if their net
flows match. That is a real blind spot in this hypothesis, not a rounding error. Closing
it costs a funded PumpPortal key — see `docs/DATA_SOURCES.md`.

Two conditions §12 does not ask for were added because the data made them necessary: an
**activity gate** (a dormant token has a velocity of exactly zero for reasons that are not
momentum) and **data-quality gates** (enough observations for a second derivative, and a
recent enough reading).

## Conjunction, not a score

All nine clauses must hold. A weighted score needs weights, weights need evidence, and
there is none — Hades V1's DynamicWeightEngine combined regime, Sharpe, profit factor,
drawdown, consistency, sample size, AI and research into one number before any component
had demonstrated an edge.

Every clause is recorded whether it passed or failed, so the binding one is visible.
§17 asks how results vary with token age, liquidity and activity, and that is unanswerable
from a signal that only says "fired".

## Measured: a 200-second live run

`scripts/run_signals_smoke.py 200`, real sources, 12 tracked tokens.

```
observations_total    211
signals_total           5
signal_rate         2.37%
tokens_with_a_signal    2   (of 12 tracked)
```

### Which clause blocked, over every observation

| Clause | Blocked | Share |
|---|---|---|
| `market_cap_velocity` | 201 | **95.3%** |
| `liquidity_sol` | 180 | 85.3% |
| `price_movement_ratio` | 152 | 72.0% |
| `seconds_since_last_trade` | 134 | 63.5% |
| `liquidity_velocity` | 50 | 23.7% |
| `market_cap_acceleration` | 44 | 20.9% |
| `observations` | 24 | 11.4% |
| `token_age` | 16 | 7.6% |

**What this actually says: most Pump.fun tokens are dead on arrival.** 85% of observations
were of tokens holding under 1 SOL; 72% were of tokens whose curve had not moved in the
window; 64% had not traded in the last 30 seconds. That is a property of the universe, not
of the thresholds — and it is the first genuinely new thing this system has learned.

It also means `market_cap_velocity` at 0.05 SOL/s is doing most of the gating, and nobody
has established that 0.05 is the right number. It is the obvious first thing for Phase 7 to
sweep.

Tracking reported `stale_observed` at 89 of 214 snapshots (42%), consistent with the Phase 3
finding that on this provider "stale" means "not trading" rather than "data is old".

## 🔴 A finding for Phase 6

Four of the five signals were on the **same token**. That is correct here — consecutive
observations of a token in a momentum regime all satisfy the conditions, and each is a
distinct, legitimate T0 for research.

It would be wrong in Phase 6. Paper trading on this stream as-is would open four positions
in the same token within a minute. **Phase 6 needs a one-position-per-token rule and/or a
cooldown**, and that belongs in the risk engine (§13's MAX OPEN POSITIONS), not here — the
signal engine's job is to report what it sees, not to decide how often that should be acted
on.

## The immutable record (spec §11)

```
SIGNAL CREATED -> FEATURE SNAPSHOT AT T0 -> IMMUTABLE STORAGE
```

Every evaluation writes a `feature_observations` row: `observation_id`, `token_address`,
`observed_at`, `feature_version`, and the full vector. The table has **no mutable column at
all** — not even an `updated_at` — and nothing in the codebase issues an UPDATE against it.
A conflict is `DO NOTHING`, never `DO UPDATE`.

`signals` points at the observation it came from rather than copying the vector, so there
is exactly one copy of the numbers behind a decision and it cannot drift.

**Every evaluation produces an observation; only some produce a signal.** That asymmetry is
the point: §17 asks how many signals there were, and the answer means nothing without how
many chances there were. The observation table is the denominator, and it is §16's research
dataset.

### Storage

At 40 tracked tokens on the default schedule that is roughly **100k observations a day at
~1 KB each — ~100 MB/day**, enough to fill a homelab rootfs in months.
`HADES_SIGNAL_OBSERVATION_MIN_INTERVAL_SECONDS` thins it. The default of 0 stores every
snapshot, which is the research ideal; the knob exists so running out of disk is a choice
rather than an outage.

## Reproduce

```bash
.venv/Scripts/python scripts/run_signals_smoke.py 200
```

Runs discovery, tracking and signals together against live sources into a throwaway SQLite
file, and prints the per-clause blocking breakdown above.
