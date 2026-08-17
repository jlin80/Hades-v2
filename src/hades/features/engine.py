"""Assembling a feature vector.

The engine is a dispatch table over ``definitions.py`` plus a version. It holds
no state, touches no clock and reads no database: given the same series and the
same ``as_of``, it returns the same vector, forever.

## Versioning

``FEATURE_VERSION`` is stamped on every vector. Change a definition and the
version must change with it, because a stored vector's meaning is fixed by the
version that produced it. Spec §11 requires the features behind a decision to
stay intact; a vector whose formulas silently changed underneath it is not
intact, it just looks it.

## No look-ahead, structurally

``compute_features`` takes ``as_of`` and immediately truncates the series to it.
Nothing downstream can see a later observation, whatever it asks for. Hades V1's
hardest lesson was that temporal leakage fails *looking like success* — great
offline metrics from a model that cannot work — so this is a property of the
call signature, not a rule to remember.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from hades.features import definitions as f
from hades.features.series import SnapshotSeries

# Bump on any change to a definition, a window, or the feature set.
FEATURE_VERSION = "1.0.0"

# Features computed once over everything known so far.
_LIFETIME: dict[str, Callable[[SnapshotSeries], float | None]] = {
    "token_age_seconds": f.token_age_seconds,
    "price_sol": f.price_sol,
    "market_cap_sol": f.market_cap_sol,
    "liquidity_sol": f.liquidity_sol,
    "reply_count": f.reply_count,
    "seconds_since_last_trade": f.seconds_since_last_trade,
    "is_graduated": f.is_graduated,
    "max_market_cap_sol": f.max_market_cap_sol,
    "drawdown_from_max": f.drawdown_from_max,
    "price_movement_ratio": f.price_movement_ratio,
    "observation_count": f.observation_count,
    "series_span_seconds": f.series_span_seconds,
    "mean_sampling_interval_seconds": f.mean_sampling_interval_seconds,
    "max_sampling_gap_seconds": f.max_sampling_gap_seconds,
}

# Features computed separately over each lookback window, suffixed with it.
_WINDOWED: dict[str, Callable[[SnapshotSeries], float | None]] = {
    "price_velocity": f.price_velocity,
    "market_cap_velocity": f.market_cap_velocity,
    "liquidity_velocity": f.liquidity_velocity,
    "reply_velocity": f.reply_velocity,
    "curve_consumption_velocity": f.curve_consumption_velocity,
    "price_return": f.price_return,
    "market_cap_return": f.market_cap_return,
    "liquidity_return": f.liquidity_return,
    "price_acceleration": f.price_acceleration,
    "market_cap_acceleration": f.market_cap_acceleration,
    "liquidity_acceleration": f.liquidity_acceleration,
    "price_movement_ratio": f.price_movement_ratio,
    "observation_count": f.observation_count,
}


@dataclass(frozen=True, slots=True)
class FeatureWindows:
    """Lookback windows, in seconds.

    30s and 60s by default. Both are several sampling intervals wide at the
    measured ~12.2s achieved rate — 30s is only two or three observations, which
    is the practical floor for an acceleration, and anything shorter would be
    computing a second derivative from noise.
    """

    seconds: tuple[float, ...] = (30.0, 60.0)


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """A computed vector, with everything needed to reproduce it."""

    token_address: str
    observed_at: datetime
    feature_version: str
    values: dict[str, float | None]

    def as_dict(self) -> dict[str, object]:
        return {
            "token_address": self.token_address,
            "observed_at": self.observed_at.isoformat(),
            "feature_version": self.feature_version,
            "features": dict(self.values),
        }


def feature_names(windows: FeatureWindows | None = None) -> list[str]:
    """Every key a vector will contain, in a stable order.

    Stable because a dataset with columns that reorder between runs is a dataset
    whose exports cannot be concatenated.
    """
    resolved = windows or FeatureWindows()
    names = list(_LIFETIME)
    for seconds in resolved.seconds:
        suffix = _window_suffix(seconds)
        names += [f"{name}_{suffix}" for name in _WINDOWED]
    names.append("freshness_seconds")
    return names


def compute_features(
    series: SnapshotSeries,
    *,
    token_address: str,
    as_of: datetime,
    windows: FeatureWindows | None = None,
) -> FeatureVector:
    """Compute every feature from the observations at or before ``as_of``.

    Any feature whose inputs are missing is None, never 0.0. A missing value and
    a zero are different facts, and only one of them is a measurement.
    """
    resolved = windows or FeatureWindows()
    visible = series.up_to(as_of)

    values: dict[str, float | None] = {
        name: compute(visible) for name, compute in _LIFETIME.items()
    }

    for seconds in resolved.seconds:
        suffix = _window_suffix(seconds)
        windowed = visible.window(as_of, seconds)
        for name, compute in _WINDOWED.items():
            values[f"{name}_{suffix}"] = compute(windowed)

    values["freshness_seconds"] = f.freshness_seconds(visible, as_of)

    return FeatureVector(
        token_address=token_address,
        observed_at=as_of,
        feature_version=FEATURE_VERSION,
        values=values,
    )


def _window_suffix(seconds: float) -> str:
    """`30.0` -> `30s`. Integral windows keep integral names."""
    return f"{int(seconds)}s" if seconds == int(seconds) else f"{seconds}s"
