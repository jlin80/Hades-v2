"""The questions spec §17 asks, as pure functions over labelled records.

    How many signals were there?      How many reached TP?  How many SL?
    Mean MFE?  Mean MAE?  Expectancy?  Profit factor?
    And how do all of those change with token age, liquidity, activity?

No machine learning. §17 is explicit that the simple questions come first, and
Hades V1 is the cautionary case: a twelve-model AI committee with a meta-model
and a dynamic weight engine, built before any single component had demonstrated
an edge.

## Unresolved records are excluded, and counted

A record whose window has not elapsed is not a timeout — it is unknown. Mixing
the two would bias every statistic toward whatever the newest tokens happened to
be doing, which is precisely the population the research is about. So
``resolved`` and ``unresolved`` are both reported, and every rate uses the
resolved count as its denominator.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from hades.outcomes.labels import BarrierLabel


@dataclass(frozen=True, slots=True)
class LabelledRecord:
    """One observation, its features, and what happened next.

    Spec §16's dataset row: TOKEN + FEATURE SNAPSHOT AT T0 + FUTURE OUTCOME.
    """

    token_address: str
    features: dict[str, float | None]
    label: BarrierLabel
    mfe: float | None
    mae: float | None
    returns: dict[str, float | None] = field(default_factory=dict)
    had_signal: bool = False

    def feature(self, name: str) -> float | None:
        return self.features.get(name)


@dataclass(frozen=True, slots=True)
class Summary:
    """§17's answers for one population of records."""

    total: int
    resolved: int
    unresolved: int
    with_signal: int

    upper: int
    lower: int
    timeout: int

    upper_rate: float | None
    lower_rate: float | None
    timeout_rate: float | None

    mean_mfe: float | None
    mean_mae: float | None
    median_mfe: float | None
    median_mae: float | None

    expectancy: float | None
    """Mean outcome per resolved record, in barrier units: an upper touch counts
    as +upper_fraction and a lower touch as -lower_fraction. Timeouts count at
    their actual terminal return where known, else zero."""

    profit_factor: float | None
    """Gross wins over gross losses. None when there were no losses, because a
    profit factor of infinity is not a measurement."""

    mean_return: dict[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "with_signal": self.with_signal,
            "upper": self.upper,
            "lower": self.lower,
            "timeout": self.timeout,
            "upper_rate": self.upper_rate,
            "lower_rate": self.lower_rate,
            "timeout_rate": self.timeout_rate,
            "mean_mfe": self.mean_mfe,
            "mean_mae": self.mean_mae,
            "expectancy": self.expectancy,
            "profit_factor": self.profit_factor,
        }


def summarise(
    records: Sequence[LabelledRecord],
    *,
    upper_fraction: float = 0.30,
    lower_fraction: float = 0.20,
    horizons: Sequence[str] = ("1m", "5m", "15m", "30m", "1h"),
) -> Summary:
    """Answer §17's questions for a population.

    The barrier fractions have to be passed in because expectancy is measured in
    the units of the labelling scheme that produced these records: a +30%/-20%
    label and a +50%/-25% label give different expectancies from the same
    market, and reporting one number without saying which scheme it came from
    would be meaningless.
    """
    resolved = [r for r in records if r.label is not BarrierLabel.UNRESOLVED]
    unresolved = len(records) - len(resolved)

    upper = sum(1 for r in resolved if r.label is BarrierLabel.UPPER)
    lower = sum(1 for r in resolved if r.label is BarrierLabel.LOWER)
    timeout = sum(1 for r in resolved if r.label is BarrierLabel.TIMEOUT)
    denominator = len(resolved)

    outcomes: list[float] = []
    for record in resolved:
        if record.label is BarrierLabel.UPPER:
            outcomes.append(upper_fraction)
        elif record.label is BarrierLabel.LOWER:
            outcomes.append(-abs(lower_fraction))
        else:
            # A timeout's value is whatever it was actually worth at the end,
            # not zero. Treating every timeout as break-even would hide a
            # strategy that times out consistently underwater.
            terminal = _last_known(record.returns, horizons)
            outcomes.append(terminal if terminal is not None else 0.0)

    wins = [value for value in outcomes if value > 0]
    losses = [-value for value in outcomes if value < 0]

    return Summary(
        total=len(records),
        resolved=denominator,
        unresolved=unresolved,
        with_signal=sum(1 for r in records if r.had_signal),
        upper=upper,
        lower=lower,
        timeout=timeout,
        upper_rate=upper / denominator if denominator else None,
        lower_rate=lower / denominator if denominator else None,
        timeout_rate=timeout / denominator if denominator else None,
        mean_mfe=_mean(r.mfe for r in resolved),
        mean_mae=_mean(r.mae for r in resolved),
        median_mfe=_median(r.mfe for r in resolved),
        median_mae=_median(r.mae for r in resolved),
        expectancy=_mean(iter(outcomes)),
        # None, not infinity: a strategy that has never lost has not been
        # measured, it has been lucky so far.
        profit_factor=(sum(wins) / sum(losses)) if losses else None,
        mean_return={
            horizon: _mean(r.returns.get(horizon) for r in resolved) for horizon in horizons
        },
    )


@dataclass(frozen=True, slots=True)
class Bucket:
    """One slice of the population, with its own summary."""

    name: str
    lower_edge: float | None
    upper_edge: float | None
    summary: Summary


def bucket_by(
    records: Sequence[LabelledRecord],
    feature: str,
    edges: Sequence[float],
    **summary_kwargs: float | Sequence[str],
) -> list[Bucket]:
    """Split by a feature and summarise each slice.

    This is the §17 question that matters most — *how do results change with
    token age, liquidity, activity?* A single aggregate number cannot answer
    whether a hypothesis works everywhere or only in one corner, and "only in
    one corner" is the far more common truth.

    Records whose feature is None form no bucket at all rather than falling into
    the first one: an unmeasured token is not a small one.
    """
    buckets: list[Bucket] = []
    bounds = [None, *edges, None]
    for index in range(len(bounds) - 1):
        low = bounds[index]
        high = bounds[index + 1]
        slice_ = [
            record
            for record in records
            if (value := record.feature(feature)) is not None
            and (low is None or value >= low)
            and (high is None or value < high)
        ]
        buckets.append(
            Bucket(
                name=_bucket_name(feature, low, high),
                lower_edge=low,
                upper_edge=high,
                summary=summarise(slice_, **summary_kwargs),  # type: ignore[arg-type]
            )
        )
    return buckets


def _bucket_name(feature: str, low: float | None, high: float | None) -> str:
    if low is None:
        return f"{feature} < {high:g}"
    if high is None:
        return f"{feature} >= {low:g}"
    return f"{low:g} <= {feature} < {high:g}"


def _clean(values: object) -> list[float]:
    return [
        value
        for value in values  # type: ignore[attr-defined]
        if value is not None and not math.isnan(value)
    ]


def _mean(values: object) -> float | None:
    cleaned = _clean(values)
    return statistics.fmean(cleaned) if cleaned else None


def _median(values: object) -> float | None:
    cleaned = _clean(values)
    return statistics.median(cleaned) if cleaned else None


def _last_known(returns: dict[str, float | None], horizons: Sequence[str]) -> float | None:
    """The longest horizon that actually has a value."""
    for horizon in reversed(list(horizons)):
        value = returns.get(horizon)
        if value is not None:
            return value
    return None
