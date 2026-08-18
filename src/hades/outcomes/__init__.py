"""Outcome engine (Phase 7).

What happened *after* each observation — returns, MFE/MAE and triple-barrier
labels. Spec §15: recorded for every observation, whether or not a signal fired
and whether or not a trade was taken.

That last part is the whole point. Labelling only the observations we acted on
would produce a dataset that can describe our trades and cannot answer whether
the hypothesis is any good, because it would have no counterfactual.
"""

from hades.outcomes.labels import (
    RETURN_HORIZONS,
    BarrierConfig,
    BarrierLabel,
    Outcome,
    compute_outcome,
)

__all__ = [
    "RETURN_HORIZONS",
    "BarrierConfig",
    "BarrierLabel",
    "Outcome",
    "compute_outcome",
]
