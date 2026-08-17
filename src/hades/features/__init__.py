"""Quantitative features (Phase 4).

Pure functions over a snapshot series. No I/O, no clock, no database — so every
number is reproducible from stored data, which is what spec §11 requires of the
features behind a decision.
"""

from hades.features.engine import FEATURE_VERSION, FeatureWindows, compute_features
from hades.features.series import Observation, SnapshotSeries

__all__ = [
    "FEATURE_VERSION",
    "FeatureWindows",
    "Observation",
    "SnapshotSeries",
    "compute_features",
]
