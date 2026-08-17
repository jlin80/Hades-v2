# Features — Phase 4

`feature_version` **1.0.0** · 41 features · defined in `src/hades/features/definitions.py`

Spec §10 requires every feature to have a clear mathematical definition, be documented,
be reproducible, and state what data it uses. Each function carries its own definition in
its docstring; this file is the map and the reasoning.

Everything here is a **pure function of a snapshot series**. No I/O, no clock, no
database — so any number in the dataset can be recomputed and checked by hand.

## The two guarantees

**No look-ahead, structurally.** `compute_features(series, as_of=...)` truncates the
series to `as_of` before anything else runs. A later observation cannot reach a feature
even by accident, because nothing downstream is given access to one.

This is the guarantee worth the most care, because temporal leakage is the one defect that
**fails looking like success**: excellent offline metrics from a model that cannot work.
`test_future_observations_cannot_change_a_vector` inserts a violent price move *after* the
decision point and asserts the vector is byte-identical.

**Missing is None, never 0.0.** A zero velocity claims the price did not move. Not knowing
whether it moved is a different fact, and collapsing the two would let unmeasured periods
masquerade as calm ones.

## Rates use observed time, never the configured interval

Phase 3 measured the achieved sampling interval at **~12.2 s against a 10 s target**. Every
rate here divides by the real elapsed time between the observations it used. A velocity
computed against the configured interval would be ~20% wrong, consistently, in a direction
nobody would notice.

The vector also carries what it was computed from, so research can tell a well-sampled
window from a sparse one: `observation_count`, `series_span_seconds`,
`mean_sampling_interval_seconds`, `max_sampling_gap_seconds`.

## The feature set

### Point features — the latest observation

| Feature | Definition | Source |
|---|---|---|
| `token_age_seconds` | `observed_at - created_at` | snapshot |
| `price_sol` | spot price on the curve | derived from virtual reserves |
| `market_cap_sol` | `price × total_supply` | derived |
| `liquidity_sol` | SOL held by the curve | `real_sol_reserves` |
| `reply_count` | comments on the pump.fun page | snapshot |
| `seconds_since_last_trade` | `observed_at - last_trade_at` | snapshot |
| `is_graduated` | 1.0 once the curve completes | `is_complete` |

`liquidity_sol` is not an approximation. A pre-graduation token has no AMM pool, so the
SOL in the curve *is* its liquidity.

`seconds_since_last_trade` is what Phase 3's `is_stale` was actually measuring, now named
for what it is — see `docs/DATA_SOURCES.md`.

### Windowed features — computed over 30 s and 60 s lookbacks

Suffixed `_30s` / `_60s`. Both windows are several sampling intervals wide at the measured
rate; 30 s is about three observations, which is the practical floor for a second
derivative.

| Feature | Definition |
|---|---|
| `price_velocity` | `Δprice / Δt` (SOL/token/s) |
| `market_cap_velocity` | `Δmcap / Δt` (SOL/s) |
| `liquidity_velocity` | `Δreal_sol / Δt` — **net** SOL flow, signed |
| `curve_consumption_velocity` | tokens leaving the curve per second |
| `reply_velocity` | new comments per second |
| `price_return`, `market_cap_return`, `liquidity_return` | `(end - start) / start` |
| `price_acceleration`, `market_cap_acceleration`, `liquidity_acceleration` | change in velocity per second |
| `price_movement_ratio` | fraction of intervals in which the price changed |
| `observation_count` | how many observations the window held |

`curve_consumption_velocity` inverts the sign of the raw reserve on purpose: the reserve
*falls* as the token is bought, and a feature that goes down when activity goes up is one
that will eventually be read backwards.

### Path features

`max_market_cap_sol` and `drawdown_from_max` — the fractional fall from the peak seen *so
far*, which is a peak over visible history only, never a future high.

### 🔴 Acceleration: a bug that only real data found

An earlier version split the series at its **midpoint in time**. It reads better and it
almost never worked: real timestamps are uneven, so the computed midpoint lands between
observations and one half is left with a single point — too few for a velocity.

On a live token with three observations in the 30-second window, **every acceleration
returned None**. The unit tests passed throughout, because their fixtures used round
10-second offsets where the midpoint coincides with an observation exactly.

Now the split is by index, with the middle observation shared, so three points give two
halves of two. That is safe despite uneven spacing because each half's velocity divides by
its own observed elapsed time. Two regression tests pin it, one using the exact uneven
timestamps that failed.

## What is deliberately missing, and why

Spec §10 lists these. None are implemented, and none can be:

| Feature | Why not |
|---|---|
| `buy_sell_ratio` | needs per-trade data |
| `buy_volume_ratio` | needs the gross buy/sell split |
| `buyer_velocity`, `seller_velocity` | need unique trader identities |
| `buyer_acceleration` | same |
| `transaction_velocity`, `transaction_acceleration` | need trade counts |
| `bonding_curve_progress` | curve constants unverified — see `DECISIONS.md` D14 |

Phase 1 established that no free source supplies per-trade data: GeckoTerminal has unique
buyers but rate-limits at ~10–30 calls/min, and PumpPortal's trade stream requires an API
key funded with 0.02 SOL.

**Two features stand in for them, and neither pretends to be the real thing.**

`liquidity_velocity` is net SOL flow. It carries direction — positive is net buying
pressure — but it is a *net* figure: gross buys and gross sells cannot be separated from
it. That separation is precisely what the missing trade stream would have provided.

`price_movement_ratio` measures how often the curve moved. The curve only moves when
someone trades, so it is a genuine activity signal — but it measures *whether* trades
happened in an interval, not how many, and it saturates at 1.0 exactly when a token is
busiest. That saturation is at the top of the range the early-momentum hypothesis cares
about, so it must not be treated as a trade count.

## Consequence for Phase 5

Spec §12 sketches the first hypothesis as *"buying activity accelerating + volume
increasing + sell pressure below threshold"*. As written, that needs the features above
that do not exist.

What the vector can support today is a momentum hypothesis built on **price, market cap
and net-flow velocity and acceleration**, with `price_movement_ratio` as an activity gate
and `seconds_since_last_trade` as an inactivity filter — plus `reply_velocity`, a free
social signal Hades V1 collected for months and never used.

That is a legitimate hypothesis. It is **not** the one in §12, and Phase 5 must say so
rather than quietly substitute it.

## Reproduce

```bash
.venv/Scripts/python scripts/run_features_demo.py 120
```

Collects a live token straight off the WebSocket, takes real snapshots, and prints the
vector at three points in its life.
