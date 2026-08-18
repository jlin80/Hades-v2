"""Exit rules, including the ordering assumption that shapes every result."""

from __future__ import annotations

import pytest

from hades.db.models import ExitReason
from hades.paper.exits import ExitEvaluation, ExitRules, decide_exit

RULES = ExitRules()
ENTRY = 1.0


def evaluate(
    *,
    price: float | None,
    peak: float | None = None,
    held: float = 10.0,
    risk_exit: bool = False,
    rules: ExitRules = RULES,
) -> ExitEvaluation:
    return decide_exit(
        entry_price=ENTRY,
        current_price=price,
        peak_price=peak,
        held_seconds=held,
        rules=rules,
        risk_exit=risk_exit,
    )


class TestHolding:
    def test_a_flat_position_stays_open(self) -> None:
        assert evaluate(price=1.0).should_exit is False

    def test_a_small_gain_stays_open(self) -> None:
        assert evaluate(price=1.1).should_exit is False

    def test_a_small_loss_stays_open(self) -> None:
        assert evaluate(price=0.95).should_exit is False


class TestExitReasons:
    def test_take_profit(self) -> None:
        result = evaluate(price=1.31)
        assert result.reason is ExitReason.TAKE_PROFIT
        assert result.return_fraction == pytest.approx(0.31)

    def test_stop_loss(self) -> None:
        result = evaluate(price=0.79)
        assert result.reason is ExitReason.STOP_LOSS

    def test_timeout(self) -> None:
        result = evaluate(price=1.05, held=999.0)
        assert result.reason is ExitReason.TIMEOUT

    def test_risk_exit_beats_everything(self) -> None:
        """A portfolio breach is about the account, not the position."""
        result = evaluate(price=1.31, risk_exit=True)
        assert result.reason is ExitReason.RISK_EXIT


class TestTrailingStop:
    def test_arms_only_after_a_real_run(self) -> None:
        """Without arming, a trailing stop is a tighter stop loss.

        It would trigger on ordinary noise immediately after entry, before the
        position has done anything.
        """
        # Peak barely above entry: the trailing stop is not armed, so a 15% fall
        # from that peak does not close the position.
        result = evaluate(price=0.9, peak=1.05)
        assert result.should_exit is False

    def test_fires_once_armed_and_the_price_falls_back(self) -> None:
        # Ran to +40%, then gave back 20% of the peak.
        result = evaluate(price=1.12, peak=1.4)
        assert result.reason is ExitReason.TRAILING_STOP

    def test_a_new_high_never_triggers_it(self) -> None:
        result = evaluate(price=1.25, peak=1.2)
        assert result.should_exit is False


class TestOrderingIsConservative:
    def test_stop_loss_wins_when_a_snapshot_satisfies_both(self) -> None:
        """The most consequential assumption in the simulator.

        Between two snapshots ~12s apart the price may have hit both levels, and
        we cannot see which came first. Recording the loss assumes the worse
        path, so results are understated rather than flattered.
        """
        # Take profit at +30%, stop loss at -20%. A price below the stop after a
        # peak far above the target satisfies both readings of the interval.
        result = decide_exit(
            entry_price=1.0,
            current_price=0.75,
            peak_price=1.5,
            held_seconds=10.0,
            rules=RULES,
        )
        assert result.reason is ExitReason.STOP_LOSS

    def test_stop_loss_also_wins_over_a_trailing_stop(self) -> None:
        result = decide_exit(
            entry_price=1.0,
            current_price=0.7,
            peak_price=1.4,
            held_seconds=10.0,
            rules=RULES,
        )
        assert result.reason is ExitReason.STOP_LOSS


class TestMissingDataDoesNotClose:
    def test_an_unpriceable_snapshot_leaves_the_position_open(self) -> None:
        """We cannot price an exit we cannot see.

        Inventing one would put a fabricated number straight into the PnL.
        """
        assert evaluate(price=None).should_exit is False
        assert evaluate(price=0.0).should_exit is False

    def test_a_risk_exit_still_closes_without_a_price(self) -> None:
        """The account-level breach does not wait for a quote."""
        assert evaluate(price=None, risk_exit=True).reason is ExitReason.RISK_EXIT

    def test_a_zero_entry_price_is_not_evaluable(self) -> None:
        result = decide_exit(
            entry_price=0.0,
            current_price=1.0,
            peak_price=None,
            held_seconds=10.0,
            rules=RULES,
        )
        assert result.should_exit is False


class TestConfigurability:
    def test_tighter_rules_close_sooner(self) -> None:
        tight = ExitRules(take_profit_fraction=0.05, stop_loss_fraction=0.02)
        assert evaluate(price=1.06, rules=tight).reason is ExitReason.TAKE_PROFIT
        assert evaluate(price=0.97, rules=tight).reason is ExitReason.STOP_LOSS
