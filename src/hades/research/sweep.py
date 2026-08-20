"""Re-evaluating exit barriers against a collected dataset.

Twenty numbers in ``hades.config`` decide what this system trades, and every one
is documented as "a plausible starting point, NOT a value derived from evidence".
This module is what lets a few of them stop being guesses.

The trick is that it needs no re-run. ``mfe`` and ``mae`` are the extremes of the
path the price actually took before a row's time barrier, so whether some *other*
take-profit and stop-loss pair would have been touched is already a fact about
data on disk. Sweeping them costs one pass over the dataset rather than a
re-collection per candidate.

What that does not extend to is anything path-dependent or portfolio-level — a
trailing stop needs the shape of the path and not only its extremes, and the open
position limits bind on a sequence of trades sharing one balance, which a dataset
of independent rows does not contain. ``scripts/sweep_phase7.py`` enumerates
those explicitly rather than leaving their absence to be noticed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hades.research.analytics import LabelledRecord

RETURN_HORIZONS: tuple[str, ...] = ("1h", "30m", "15m", "5m", "1m")


@dataclass(frozen=True, slots=True)
class BarrierResult:
    """What one take-profit / stop-loss pair would have produced."""

    take_profit: float
    stop_loss: float
    resolved: int
    """Rows that touched a barrier. Rows that touched neither are counted in
    ``expectancy`` at their terminal return but are not 'resolved' by it."""

    wins: int
    losses: int
    expectancy: float | None
    """Mean outcome per record, in return units. None for an empty population."""

    @property
    def win_rate(self) -> float | None:
        """Wins over rows that resolved. None when nothing touched a barrier."""
        return self.wins / self.resolved if self.resolved else None


def evaluate_barriers(
    records: Sequence[LabelledRecord], *, take_profit: float, stop_loss: float
) -> BarrierResult:
    """Replay a barrier pair over already-labelled rows.

    **A row whose path touched both barriers counts as a loss.** That is not a
    convenience: it is the same rule the live simulator applies in
    ``hades.paper.service``, where between two snapshots the price may have
    visited both levels and nothing in the data says which came first, so the
    worse path is assumed. A sweep that broke the tie the other way would report
    an edge manufactured entirely by its own tie-break, and would do it most
    strongly for the widest, most ambiguous barrier pairs — exactly the cells a
    reader would be most tempted by.
    """
    wins = losses = 0
    total = 0.0
    for record in records:
        mfe, mae = record.mfe, record.mae
        if mfe is None or mae is None:
            continue
        if mae <= -stop_loss:
            losses += 1
            total -= stop_loss
        elif mfe >= take_profit:
            wins += 1
            total += take_profit
        else:
            terminal = terminal_return(record)
            if terminal is not None:
                total += terminal
    return BarrierResult(
        take_profit=take_profit,
        stop_loss=stop_loss,
        resolved=wins + losses,
        wins=wins,
        losses=losses,
        expectancy=total / len(records) if records else None,
    )


def terminal_return(record: LabelledRecord) -> float | None:
    """The row's return at the longest horizon that has a value.

    Longest first: a row that reached an hour says more about where it ended
    than its one-minute return does.
    """
    for horizon in RETURN_HORIZONS:
        value = record.returns.get(horizon)
        if value is not None:
            return value
    return None
