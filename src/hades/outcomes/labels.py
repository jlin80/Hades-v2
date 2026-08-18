"""Labelling what happened after an observation. Pure.

Spec §15 asks for returns at fixed horizons, MFE/MAE, and triple-barrier labels
with configurable barriers.

## Forward-looking here is correct

Phase 4 spent its design budget making it impossible for a *feature* to see the
future. This module does the opposite on purpose: an outcome is by definition
what happened next, and a label computed from anything else would not be a
label.

The separation is what keeps both honest. Features come from
``series.up_to(t)``; outcomes come from ``series`` after ``t``; nothing joins
them except a stored ``observation_id``. A pipeline that computed both from the
same window is exactly the leak that produces excellent offline metrics from a
model that cannot work.

## A label is only valid once its window has actually elapsed

An observation whose horizon has not passed is **UNRESOLVED**, not a timeout. A
timeout means "neither barrier was touched in the allotted time"; unresolved
means "we do not know yet". Collapsing the two would fill the dataset with
label noise that looks like real evidence, and it would be worst for the newest
tokens — the ones the research is most about.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from hades.features.series import SnapshotSeries

# Spec §15's horizons.
RETURN_HORIZONS: tuple[tuple[str, float], ...] = (
    ("1m", 60.0),
    ("5m", 300.0),
    ("15m", 900.0),
    ("30m", 1800.0),
    ("1h", 3600.0),
)


class BarrierLabel(enum.StrEnum):
    """Which barrier was touched first."""

    UPPER = "UPPER"
    LOWER = "LOWER"
    TIMEOUT = "TIMEOUT"
    UNRESOLVED = "UNRESOLVED"
    """Not enough elapsed time or data yet. Distinct from TIMEOUT on purpose."""


@dataclass(frozen=True, slots=True)
class BarrierConfig:
    """One labelling scheme. Several can be applied to the same observation.

    Spec §15 asks for multiple configurations, because "did +30% happen before
    -20%?" is one question and the dataset should be able to answer several.
    """

    name: str
    upper_fraction: float = 0.30
    lower_fraction: float = 0.20
    time_barrier_seconds: float = 3600.0


DEFAULT_BARRIERS: tuple[BarrierConfig, ...] = (
    BarrierConfig(name="tp30_sl20_1h", upper_fraction=0.30, lower_fraction=0.20),
    BarrierConfig(name="tp50_sl25_1h", upper_fraction=0.50, lower_fraction=0.25),
    BarrierConfig(
        name="tp20_sl10_15m",
        upper_fraction=0.20,
        lower_fraction=0.10,
        time_barrier_seconds=900.0,
    ),
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened after one observation."""

    returns: dict[str, float | None]
    """Fractional return at each horizon, or None where the horizon has not
    elapsed or no observation exists near it."""

    mfe: float | None
    """Maximum favourable excursion: the best unrealised return reached."""

    mae: float | None
    """Maximum adverse excursion: the worst. Negative or zero."""

    mfe_at_seconds: float | None
    mae_at_seconds: float | None

    label: BarrierLabel
    barrier_hit_at_seconds: float | None
    config_name: str

    observations_after: int
    elapsed_seconds: float
    """How much time the forward series actually covers."""

    is_final: bool
    """True once the label can no longer change: a barrier was touched, or the
    time barrier has fully elapsed."""


def compute_outcome(
    series: SnapshotSeries,
    *,
    at: datetime,
    config: BarrierConfig,
    now: datetime,
) -> Outcome | None:
    """Label the observation at ``at`` using everything after it.

    ``now`` is what makes an unelapsed horizon distinguishable from a missing
    one: a return is None because the time has not passed, not because the data
    is bad. Returns None when there is no priced observation at ``at`` — an
    outcome with no starting price is not an outcome.
    """
    anchor = _price_at(series, at)
    if anchor is None or anchor <= 0:
        return None

    forward = [
        observation
        for observation in series.observations
        if observation.observed_at > at and observation.price_sol is not None
    ]
    elapsed_real = (now - at).total_seconds()

    returns: dict[str, float | None] = {}
    for name, horizon in RETURN_HORIZONS:
        # The horizon has to have actually passed before a return means
        # anything. Otherwise "return_1h" on a ten-minute-old token would be a
        # ten-minute return wearing an hour's name.
        if elapsed_real < horizon:
            returns[name] = None
            continue
        target = at + timedelta(seconds=horizon)
        price = _price_at(SnapshotSeries(list(forward)), target)
        returns[name] = None if price is None else (price - anchor) / anchor

    mfe: float | None = None
    mae: float | None = None
    mfe_at: float | None = None
    mae_at: float | None = None
    label = BarrierLabel.UNRESOLVED
    hit_at: float | None = None

    for observation in forward:
        offset = (observation.observed_at - at).total_seconds()
        if offset > config.time_barrier_seconds:
            break

        price = observation.price_sol
        if price is None:  # pragma: no cover — filtered when building `forward`
            continue
        excursion = (price - anchor) / anchor

        if mfe is None or excursion > mfe:
            mfe, mfe_at = excursion, offset
        if mae is None or excursion < mae:
            mae, mae_at = excursion, offset

        if label is BarrierLabel.UNRESOLVED:
            # Lower barrier first, for the same reason the exit rules check the
            # stop loss first: between two snapshots the price may have touched
            # both, we cannot see which came first, and assuming the worse path
            # understates results rather than flattering them.
            if excursion <= -abs(config.lower_fraction):
                label, hit_at = BarrierLabel.LOWER, offset
            elif excursion >= config.upper_fraction:
                label, hit_at = BarrierLabel.UPPER, offset

    # A barrier touch is final. Otherwise the label only becomes TIMEOUT once
    # the whole window has genuinely elapsed -- before that it is UNRESOLVED,
    # which is a different claim.
    if label is BarrierLabel.UNRESOLVED and elapsed_real >= config.time_barrier_seconds:
        label = BarrierLabel.TIMEOUT

    return Outcome(
        returns=returns,
        mfe=mfe,
        mae=mae,
        mfe_at_seconds=mfe_at,
        mae_at_seconds=mae_at,
        label=label,
        barrier_hit_at_seconds=hit_at,
        config_name=config.name,
        observations_after=len(forward),
        elapsed_seconds=elapsed_real,
        is_final=label in (BarrierLabel.UPPER, BarrierLabel.LOWER, BarrierLabel.TIMEOUT),
    )


def _price_at(series: SnapshotSeries, moment: datetime) -> float | None:
    """The most recent priced observation at or before ``moment``.

    Last-known-value rather than interpolation. An interpolated price is a
    number nobody observed, and putting one into a label makes the label partly
    fiction.
    """
    for observation in reversed(series.up_to(moment).observations):
        if observation.price_sol is not None:
            return observation.price_sol
    return None
