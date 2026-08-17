"""Feature definitions.

Every function here is pure, takes a series and returns a number or None, and
carries its own mathematical definition in its docstring. Spec §10: a feature
must have a clear definition, be documented, be reproducible, and state what
data it uses.

**None is the answer whenever the inputs are missing.** Not 0.0 — a zero
velocity is a claim that the price did not move, and that is a different fact
from not knowing whether it did.

## What is deliberately absent

`buy_sell_ratio`, `buy_volume_ratio`, `buyer_velocity`, `seller_velocity`,
`transaction_velocity`, `buyer_acceleration` and `transaction_acceleration` are
all in spec §10 and none of them are here. They need per-trade data, and Phase 1
established that no free source supplies it: GeckoTerminal has unique buyers but
rate-limits at ~10-30 calls/min, and PumpPortal's trade stream requires an API
key funded with 0.02 SOL. See `docs/DATA_SOURCES.md`.

`price_movement_ratio` below is the honest partial substitute: it measures how
often the curve moved, which is a consequence of trading, without pretending to
count trades.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import datetime

from hades.features.series import Observation, SnapshotSeries


def _rate(
    start_value: float | None, end_value: float | None, elapsed: float | None
) -> float | None:
    """(end - start) / elapsed, or None if any input is missing."""
    if start_value is None or end_value is None or not elapsed:
        return None
    return (end_value - start_value) / elapsed


def _ratio_change(start_value: float | None, end_value: float | None) -> float | None:
    """(end - start) / start. None when start is missing or zero.

    Zero start is None rather than infinity: a token whose price was zero and is
    now positive has an undefined return, and infinity in a feature vector
    propagates into every statistic computed from it.
    """
    if start_value is None or end_value is None or start_value == 0:
        return None
    return (end_value - start_value) / start_value


# --- Point features ---------------------------------------------------------


def token_age_seconds(series: SnapshotSeries) -> float | None:
    """Age of the token at the latest observation, in seconds.

    Uses: ``token_age_seconds`` on the snapshot, itself ``observed_at -
    created_at``. The single most important feature: every window in this
    system is defined relative to it, and a token without ``created_at`` cannot
    be tracked at all.
    """
    last = series.last
    return last.token_age_seconds if last else None


def price_sol(series: SnapshotSeries) -> float | None:
    """Latest spot price on the bonding curve, in SOL per token.

    Uses: derived from ``virtual_sol_reserves / virtual_token_reserves`` at
    snapshot time — see ``hades.tracking.derive``, verified against the
    provider's own market cap to five decimals.
    """
    last = series.last
    return last.price_sol if last else None


def market_cap_sol(series: SnapshotSeries) -> float | None:
    """Latest market capitalisation in SOL. Uses: ``price_sol * total_supply``."""
    last = series.last
    return last.market_cap_sol if last else None


def liquidity_sol(series: SnapshotSeries) -> float | None:
    """SOL actually in the bonding curve at the latest observation.

    Uses: ``real_sol_reserves``. For a pre-graduation token there is no AMM
    pool, so the SOL in the curve *is* the liquidity — this is the correct
    definition here, not an approximation of one.
    """
    last = series.last
    return last.liquidity_sol if last else None


def reply_count(series: SnapshotSeries) -> float | None:
    """Comments on the token's pump.fun page at the latest observation.

    Uses: ``reply_count``. The only social signal any free source gives us, and
    Hades V1 collected it for months without ever using it.
    """
    last = series.last
    return float(last.reply_count) if last and last.reply_count is not None else None


def seconds_since_last_trade(series: SnapshotSeries) -> float | None:
    """Time between the last trade and the latest observation.

    Uses: ``observed_at - last_trade_at``.

    This is what Phase 3's ``is_stale`` was actually measuring. pump.fun's
    ``updated_at`` tracks the last record change, which is essentially the last
    trade, so a token nobody trades reports a growing "data age" while its data
    stays accurate. Named for what it measures.
    """
    last = series.last
    if last is None or last.last_trade_at is None:
        return None
    return (last.observed_at - last.last_trade_at).total_seconds()


def is_graduated(series: SnapshotSeries) -> float | None:
    """1.0 if the token has completed its bonding curve, else 0.0.

    Uses: ``is_complete``. A float rather than a bool so the vector stays
    numerically homogeneous.
    """
    last = series.last
    if last is None or last.is_complete is None:
        return None
    return 1.0 if last.is_complete else 0.0


# --- Rate features ----------------------------------------------------------


def _endpoints(series: SnapshotSeries) -> tuple[Observation, Observation, float] | None:
    """First and last observation plus the real elapsed seconds between them."""
    first, last = series.first, series.last
    span = series.span_seconds()
    if first is None or last is None or not span:
        return None
    return first, last, span


def price_velocity(series: SnapshotSeries) -> float | None:
    """Change in price per second across the series.

    ``(price_last - price_first) / (t_last - t_first)``, in SOL per token per
    second. Divides by the *observed* elapsed time, never by the configured
    sampling interval — measured, those differ by ~20%.
    """
    ends = _endpoints(series)
    return None if ends is None else _rate(ends[0].price_sol, ends[1].price_sol, ends[2])


def market_cap_velocity(series: SnapshotSeries) -> float | None:
    """Change in market cap per second, in SOL/s. Same form as price_velocity."""
    ends = _endpoints(series)
    return None if ends is None else _rate(ends[0].market_cap_sol, ends[1].market_cap_sol, ends[2])


def liquidity_velocity(series: SnapshotSeries) -> float | None:
    """Net SOL entering the curve per second.

    ``(real_sol_last - real_sol_first) / elapsed``. Signed, so direction is
    known — positive is net buying pressure.

    This is as close as free data gets to buy/sell flow. It is a *net* figure:
    gross buys and gross sells cannot be separated from it, which is exactly
    what the missing trade stream would have provided.
    """
    ends = _endpoints(series)
    return None if ends is None else _rate(ends[0].liquidity_sol, ends[1].liquidity_sol, ends[2])


def reply_velocity(series: SnapshotSeries) -> float | None:
    """New comments per second across the series."""
    ends = _endpoints(series)
    first, last, span = ends if ends else (None, None, None)
    if first is None or last is None:
        return None
    start = float(first.reply_count) if first.reply_count is not None else None
    end = float(last.reply_count) if last.reply_count is not None else None
    return _rate(start, end, span)


def curve_consumption_velocity(series: SnapshotSeries) -> float | None:
    """Tokens leaving the bonding curve per second.

    ``(real_token_first - real_token_last) / elapsed``, in base units per
    second. Positive means tokens are being bought out of the curve.

    Sign is inverted relative to the raw column on purpose: the raw reserve
    *falls* as the token is bought, and a feature that goes down when activity
    goes up is a feature that will eventually be read backwards.
    """
    ends = _endpoints(series)
    if ends is None:
        return None
    first, last, span = ends
    if first.real_token_reserves is None or last.real_token_reserves is None:
        return None
    return (first.real_token_reserves - last.real_token_reserves) / span


# --- Return features --------------------------------------------------------


def price_return(series: SnapshotSeries) -> float | None:
    """Fractional price change across the series. ``(p_last - p_first)/p_first``."""
    ends = _endpoints(series)
    return None if ends is None else _ratio_change(ends[0].price_sol, ends[1].price_sol)


def market_cap_return(series: SnapshotSeries) -> float | None:
    """Fractional market-cap change across the series."""
    ends = _endpoints(series)
    return None if ends is None else _ratio_change(ends[0].market_cap_sol, ends[1].market_cap_sol)


def liquidity_return(series: SnapshotSeries) -> float | None:
    """Fractional change in SOL held by the curve across the series."""
    ends = _endpoints(series)
    return None if ends is None else _ratio_change(ends[0].liquidity_sol, ends[1].liquidity_sol)


# --- Acceleration -----------------------------------------------------------


def _acceleration(
    series: SnapshotSeries, velocity: Callable[[SnapshotSeries], float | None]
) -> float | None:
    """Change in a velocity between the first and second halves of the series.

    Needs at least three observations: a velocity needs two, and a change in
    velocity needs two velocities.

    Split by index at the middle, with the middle observation shared by both
    halves, so three points give two halves of two. An earlier version split at
    the midpoint *in time* — which reads better, and was measured on real data
    to almost never work: real timestamps are uneven, so the computed midpoint
    falls between observations and one half is left holding a single point.
    Every 30-second acceleration came back None on a live token that had three
    observations in the window.

    Splitting by index is safe here because each half's velocity is divided by
    its own *observed* elapsed time, so two halves of different duration are
    still directly comparable.
    """
    observations = series.observations
    if len(observations) < 3:
        return None

    middle = len(observations) // 2
    early = SnapshotSeries(list(observations[: middle + 1]))
    late = SnapshotSeries(list(observations[middle:]))
    if len(early) < 2 or len(late) < 2:
        return None

    early_rate = velocity(early)
    late_rate = velocity(late)
    early_start, late_start = early.first, late.first
    if early_rate is None or late_rate is None or early_start is None or late_start is None:
        return None

    gap = (late_start.observed_at - early_start.observed_at).total_seconds()
    if not gap:
        return None
    return (late_rate - early_rate) / gap


def price_acceleration(series: SnapshotSeries) -> float | None:
    """Change in price velocity per second, in SOL/token/s²."""
    return _acceleration(series, price_velocity)


def market_cap_acceleration(series: SnapshotSeries) -> float | None:
    """Change in market-cap velocity per second, in SOL/s²."""
    return _acceleration(series, market_cap_velocity)


def liquidity_acceleration(series: SnapshotSeries) -> float | None:
    """Change in net SOL flow per second, in SOL/s².

    The closest computable analogue of §10's ``buyer_acceleration``: it captures
    inflow accelerating, without claiming to know how many buyers produced it.
    """
    return _acceleration(series, liquidity_velocity)


# --- Path features ----------------------------------------------------------


def max_market_cap_sol(series: SnapshotSeries) -> float | None:
    """Highest market cap observed so far in the series."""
    values = [o.market_cap_sol for o in series.observations if o.market_cap_sol is not None]
    return max(values) if values else None


def drawdown_from_max(series: SnapshotSeries) -> float | None:
    """Fractional fall from the peak market cap seen so far.

    ``(max_so_far - current) / max_so_far``, so 0.0 is at the peak and 0.4 is
    40% below it. Non-negative by construction. Uses only observations up to the
    present, so it never peeks at a future high.
    """
    peak = max_market_cap_sol(series)
    current = market_cap_sol(series)
    if peak is None or current is None or peak == 0:
        return None
    return (peak - current) / peak


def price_movement_ratio(series: SnapshotSeries) -> float | None:
    """Fraction of consecutive intervals in which the price changed.

    ``intervals_with_a_price_change / total_intervals``, in [0, 1]. Needs at
    least two observations.

    This is the honest partial substitute for §10's ``transaction_velocity``.
    The curve only moves when someone trades, so this measures trading activity
    — but it measures *whether* trades happened per interval, not how many. It
    saturates at 1.0 exactly when the token is busiest, which is the regime the
    early-momentum hypothesis cares about, so it must not be mistaken for a
    trade count.
    """
    observations = series.observations
    if len(observations) < 2:
        return None

    intervals = 0
    moved = 0
    for previous, current in itertools.pairwise(observations):
        if previous.price_sol is None or current.price_sol is None:
            continue
        intervals += 1
        if previous.price_sol != current.price_sol:
            moved += 1
    return moved / intervals if intervals else None


# --- Data-quality features --------------------------------------------------
#
# Not descriptions of the market — descriptions of how much the market features
# can be trusted. A velocity computed from two observations 40 seconds apart is
# a different quantity from one computed from six observations 10 seconds apart,
# and research needs to be able to tell them apart.


def observation_count(series: SnapshotSeries) -> float | None:
    """How many observations the vector was computed from."""
    return float(len(series))


def series_span_seconds(series: SnapshotSeries) -> float | None:
    """Elapsed time the series actually covers."""
    return series.span_seconds()


def mean_sampling_interval_seconds(series: SnapshotSeries) -> float | None:
    """Average real gap between observations.

    Measured in Phase 3 at ~12.2s against a configured 10s, so this is not the
    configured value and should never be assumed to be.
    """
    span = series.span_seconds()
    if span is None or len(series) < 2:
        return None
    return span / (len(series) - 1)


def max_sampling_gap_seconds(series: SnapshotSeries) -> float | None:
    """Largest gap between consecutive observations.

    A large value means the series has a hole — a provider outage, a rate limit,
    a restart — and any velocity spanning that hole is an average over a period
    we did not actually watch.
    """
    times = [o.observed_at for o in series.observations]
    if len(times) < 2:
        return None
    return max((later - earlier).total_seconds() for earlier, later in itertools.pairwise(times))


def freshness_seconds(series: SnapshotSeries, as_of: datetime) -> float | None:
    """Age of the newest observation relative to ``as_of``.

    Spec §13 rejects decisions made on stale data. This is the number that check
    will use — our own data age, not the provider's record age, which Phase 3
    established measures something else entirely.
    """
    last = series.last
    return None if last is None else (as_of - last.observed_at).total_seconds()
