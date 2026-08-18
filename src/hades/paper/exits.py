"""When to close a position. Pure, so every exit can be replayed exactly.

Spec §14's exit reasons, in the order they are checked. **Order matters and is
not arbitrary:** a snapshot can satisfy several rules at once, and which one is
recorded determines what §17 later reports about why trades ended.

Stop loss is checked before take profit. Between two snapshots the price may
have visited both levels and we cannot see which came first — our sampling is
~12 seconds, and a Pump.fun token moves far more often than that. Recording the
loss is the conservative reading: it assumes the worse path happened, so results
are understated rather than flattered.

This is the single most consequential assumption in the paper simulator, and it
is a **known limitation of snapshot-based backtesting**, not something this
design solves. Closing it needs per-trade data — the same gap that removes half
of spec §12's hypothesis (``docs/DATA_SOURCES.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

from hades.db.models import ExitReason


@dataclass(frozen=True, slots=True)
class ExitRules:
    """Configurable, and none of them validated by evidence yet."""

    take_profit_fraction: float = 0.30
    stop_loss_fraction: float = 0.20
    # Fall from the peak that closes a position, once it has run far enough to
    # arm. Without the arming threshold a trailing stop is just a tighter stop
    # loss, triggering on ordinary noise right after entry.
    trailing_stop_fraction: float = 0.15
    trailing_arm_fraction: float = 0.15
    max_hold_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    """Whether to close, and why."""

    should_exit: bool
    reason: ExitReason | None
    return_fraction: float | None
    drawdown_from_peak: float | None


def decide_exit(
    *,
    entry_price: float,
    current_price: float | None,
    peak_price: float | None,
    held_seconds: float,
    rules: ExitRules,
    risk_exit: bool = False,
) -> ExitEvaluation:
    """Decide whether an open position should close on this observation.

    A missing current price does **not** close the position: we cannot price an
    exit we cannot see, and inventing one would put a fabricated number into the
    PnL. The position stays open and the next observation decides.
    """
    if entry_price <= 0:
        return ExitEvaluation(False, None, None, None)

    if risk_exit:
        # Portfolio-level breach. Checked first because it is about the account,
        # not the position, and it must not be overridden by a per-trade rule.
        return ExitEvaluation(True, ExitReason.RISK_EXIT, None, None)

    if current_price is None or current_price <= 0:
        return ExitEvaluation(False, None, None, None)

    return_fraction = (current_price - entry_price) / entry_price
    peak = max(peak_price or entry_price, current_price, entry_price)
    drawdown = (peak - current_price) / peak if peak > 0 else None

    # Stop loss first. See the module docstring: between two snapshots the price
    # may have hit both levels, and recording the loss understates rather than
    # flatters.
    if return_fraction <= -abs(rules.stop_loss_fraction):
        return ExitEvaluation(True, ExitReason.STOP_LOSS, return_fraction, drawdown)

    if return_fraction >= rules.take_profit_fraction:
        return ExitEvaluation(True, ExitReason.TAKE_PROFIT, return_fraction, drawdown)

    armed = (peak - entry_price) / entry_price >= rules.trailing_arm_fraction
    if armed and drawdown is not None and drawdown >= rules.trailing_stop_fraction:
        return ExitEvaluation(True, ExitReason.TRAILING_STOP, return_fraction, drawdown)

    if held_seconds >= rules.max_hold_seconds:
        return ExitEvaluation(True, ExitReason.TIMEOUT, return_fraction, drawdown)

    return ExitEvaluation(False, None, return_fraction, drawdown)
