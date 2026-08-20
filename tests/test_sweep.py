"""Replaying barrier pairs over a collected dataset.

The tie-break is the test that matters. A row whose path touched both barriers
has to be counted as a loss, matching the live simulator, because counting it as
a win would manufacture an edge out of the ambiguity itself — and would do it
most strongly for the widest barrier pairs, which are the cells a reader scanning
a sweep grid is most drawn to.
"""

from __future__ import annotations

import pytest

from hades.outcomes.labels import BarrierLabel
from hades.research.analytics import LabelledRecord
from hades.research.sweep import evaluate_barriers, terminal_return


def record(
    *,
    mfe: float | None,
    mae: float | None,
    returns: dict[str, float | None] | None = None,
) -> LabelledRecord:
    return LabelledRecord(
        token_address="nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump",
        features={},
        label=BarrierLabel.TIMEOUT,
        mfe=mfe,
        mae=mae,
        returns=returns or {},
    )


def test_a_path_that_touched_both_barriers_counts_as_a_loss() -> None:
    """The live rule, applied to the sweep.

    Between two snapshots the price may have visited both levels and nothing
    says which came first, so the worse path is assumed. Resolving it the other
    way would report an edge produced entirely by the tie-break.
    """
    both = [record(mfe=0.60, mae=-0.40)]

    result = evaluate_barriers(both, take_profit=0.30, stop_loss=0.20)

    assert result.losses == 1
    assert result.wins == 0
    assert result.expectancy == pytest.approx(-0.20)


def test_a_clean_win_and_a_clean_loss_are_counted_as_such() -> None:
    records = [
        record(mfe=0.50, mae=-0.05),  # target only
        record(mfe=0.05, mae=-0.40),  # stop only
    ]

    result = evaluate_barriers(records, take_profit=0.30, stop_loss=0.20)

    assert (result.wins, result.losses) == (1, 1)
    assert result.win_rate == pytest.approx(0.5)
    assert result.expectancy == pytest.approx((0.30 - 0.20) / 2)


def test_a_row_that_touched_neither_barrier_scores_its_terminal_return() -> None:
    """It is not 'resolved', but it is not nothing either.

    Dropping these would silently restrict the population to rows that moved
    sharply — the same early-resolution bias the report warns about, reintroduced
    by the sweep instead of by the dataset.
    """
    quiet = [record(mfe=0.05, mae=-0.05, returns={"1h": 0.02})]

    result = evaluate_barriers(quiet, take_profit=0.30, stop_loss=0.20)

    assert result.resolved == 0
    assert result.win_rate is None
    assert result.expectancy == pytest.approx(0.02)


def test_rows_without_a_path_are_skipped_rather_than_scored_as_flat() -> None:
    """Missing is not zero — the rule the feature engine is built on.

    A row with no MFE/MAE has an unknown path, and scoring it at 0.0 would let
    unmeasured rows dilute an expectancy towards nothing.
    """
    unknown = [record(mfe=None, mae=None), record(mfe=0.50, mae=-0.05)]

    result = evaluate_barriers(unknown, take_profit=0.30, stop_loss=0.20)

    assert result.wins == 1
    # Divided by the full population, so a dataset full of unknown paths reports
    # a small expectancy rather than a confident one.
    assert result.expectancy == pytest.approx(0.30 / 2)


def test_an_empty_population_has_no_expectancy_rather_than_zero() -> None:
    result = evaluate_barriers([], take_profit=0.30, stop_loss=0.20)

    assert result.expectancy is None
    assert result.win_rate is None


def test_terminal_return_prefers_the_longest_horizon_available() -> None:
    """A row that reached an hour says more about where it ended than 1m does."""
    assert terminal_return(record(mfe=0.0, mae=0.0, returns={"1m": 0.5, "1h": 0.1})) == 0.1
    assert terminal_return(record(mfe=0.0, mae=0.0, returns={"1m": 0.5})) == 0.5
    assert terminal_return(record(mfe=0.0, mae=0.0, returns={})) is None


def test_a_wider_stop_cannot_turn_a_stopped_row_into_a_winner() -> None:
    """Sanity on the sweep's monotonicity.

    Widening the stop past the worst excursion should move a row from loss to
    win, not leave it counted twice.
    """
    row = [record(mfe=0.50, mae=-0.25)]

    tight = evaluate_barriers(row, take_profit=0.30, stop_loss=0.20)
    wide = evaluate_barriers(row, take_profit=0.30, stop_loss=0.30)

    assert (tight.wins, tight.losses) == (0, 1)
    assert (wide.wins, wide.losses) == (1, 0)
