"""The risk gates from spec §13, as pure functions.

Every signal passes through here, and nothing else may authorise a trade. Pure:
the engine is handed the portfolio state and the market state rather than
querying for them, so a decision can be replayed exactly from what was stored.

## Fail-closed

``evaluate`` wraps the checks so that **any** exception becomes a rejection
rather than an escape. Hades V1 learned this one: its Risk Manager wrapped
``_decide()`` for exactly this reason, and it is the difference between a bug
that blocks trading and a bug that permits it.

Unknown is not permission, applied throughout: a check whose input is None
fails. A missing liquidity reading is not evidence of sufficient liquidity.

## The gates

Spec §13 lists eight. All eight are here, plus one this project's own Phase 5
run made necessary — see ``max_open_per_token``.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


class RejectionReason(enum.StrEnum):
    """Why a signal was refused. STALE_SIGNAL is named by spec §13."""

    STALE_SIGNAL = "STALE_SIGNAL"
    TOKEN_TOO_OLD = "TOKEN_TOO_OLD"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    POSITION_TOO_LARGE = "POSITION_TOO_LARGE"
    TOO_MANY_OPEN_POSITIONS = "TOO_MANY_OPEN_POSITIONS"
    ALREADY_HOLDING_TOKEN = "ALREADY_HOLDING_TOKEN"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    ENGINE_ERROR = "ENGINE_ERROR"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Spec §13's limits. All configurable; none validated by evidence yet."""

    max_token_age_seconds: float = 300.0
    min_liquidity_sol: float = 1.0
    # Fraction, so 0.05 is 5%. Computed exactly from the curve, not estimated.
    max_slippage_fraction: float = 0.05
    max_position_sol: float = 0.05
    max_open_positions: int = 5
    # Not in §13, and required by measurement: Phase 5's live run produced four
    # signals on the same token inside a minute, because consecutive
    # observations of a token in a momentum regime all satisfy the hypothesis.
    # Correct for research, ruinous for position sizing.
    max_open_per_token: int = 1
    max_daily_loss_sol: float = 0.5
    max_drawdown_fraction: float = 0.25
    # Spec §13: reject decisions made on old data.
    max_data_age_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class RiskState:
    """Portfolio state at decision time. Supplied, never queried."""

    balance_sol: float
    open_positions: int
    open_for_token: int
    realised_pnl_today_sol: float
    peak_equity_sol: float
    current_equity_sol: float

    @property
    def drawdown_fraction(self) -> float | None:
        """Fractional fall from peak equity. None when there is no peak yet."""
        if self.peak_equity_sol <= 0:
            return None
        return max(0.0, (self.peak_equity_sol - self.current_equity_sol) / self.peak_equity_sol)


@dataclass(frozen=True, slots=True)
class RiskCheck:
    """One gate and whether it allowed the trade."""

    name: str
    passed: bool
    value: float | None
    limit: float | None
    reason: RejectionReason | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "value": self.value,
            "limit": self.limit,
            "reason": self.reason.value if self.reason else None,
        }


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The verdict, with the timestamps spec §13 requires on every signal."""

    approved: bool
    signal_created_at: datetime
    decision_at: datetime
    data_age_ms: float
    position_size_sol: float
    checks: tuple[RiskCheck, ...] = field(default_factory=tuple)

    @property
    def rejections(self) -> tuple[RejectionReason, ...]:
        return tuple(c.reason for c in self.checks if not c.passed and c.reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "signal_created_at": self.signal_created_at.isoformat(),
            "decision_at": self.decision_at.isoformat(),
            "data_age_ms": self.data_age_ms,
            "position_size_sol": self.position_size_sol,
            "checks": [c.as_dict() for c in self.checks],
        }


class RiskEngine:
    """The only thing that may authorise a paper trade."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        *,
        signal_created_at: datetime,
        decision_at: datetime,
        token_age_seconds: float | None,
        liquidity_sol: float | None,
        estimated_slippage: float | None,
        state: RiskState,
        requested_size_sol: float,
    ) -> RiskDecision:
        """Approve or reject. Never raises.

        Any exception becomes a rejection: a bug here must block trading, not
        permit it. V1's Risk Manager wrapped its decision function for exactly
        this reason and it is the single most valuable line in a risk engine.
        """
        data_age_ms = (decision_at - signal_created_at).total_seconds() * 1000
        try:
            checks = self._checks(
                data_age_ms=data_age_ms,
                token_age_seconds=token_age_seconds,
                liquidity_sol=liquidity_sol,
                estimated_slippage=estimated_slippage,
                state=state,
                requested_size_sol=requested_size_sol,
            )
            approved = all(check.passed for check in checks)
        except Exception:
            logger.exception("risk_engine_error")
            return RiskDecision(
                approved=False,
                signal_created_at=signal_created_at,
                decision_at=decision_at,
                data_age_ms=data_age_ms,
                position_size_sol=0.0,
                checks=(
                    RiskCheck(
                        name="engine",
                        passed=False,
                        value=None,
                        limit=None,
                        reason=RejectionReason.ENGINE_ERROR,
                    ),
                ),
            )

        size = min(requested_size_sol, self.limits.max_position_sol) if approved else 0.0
        return RiskDecision(
            approved=approved,
            signal_created_at=signal_created_at,
            decision_at=decision_at,
            data_age_ms=data_age_ms,
            position_size_sol=size,
            checks=checks,
        )

    def _checks(
        self,
        *,
        data_age_ms: float,
        token_age_seconds: float | None,
        liquidity_sol: float | None,
        estimated_slippage: float | None,
        state: RiskState,
        requested_size_sol: float,
    ) -> tuple[RiskCheck, ...]:
        limits = self.limits
        return (
            # Spec §13 names this one explicitly: a decision made on stale data
            # is rejected as STALE_SIGNAL rather than acted on.
            _at_most(
                "data_age",
                data_age_ms / 1000,
                limits.max_data_age_seconds,
                RejectionReason.STALE_SIGNAL,
            ),
            _at_most(
                "token_age",
                token_age_seconds,
                limits.max_token_age_seconds,
                RejectionReason.TOKEN_TOO_OLD,
            ),
            _at_least(
                "liquidity",
                liquidity_sol,
                limits.min_liquidity_sol,
                RejectionReason.INSUFFICIENT_LIQUIDITY,
            ),
            # Exact, from the curve — not an assumed percentage. See
            # hades.paper.curve.
            _at_most(
                "slippage",
                estimated_slippage,
                limits.max_slippage_fraction,
                RejectionReason.SLIPPAGE_TOO_HIGH,
            ),
            _at_most(
                "position_size",
                requested_size_sol,
                limits.max_position_sol,
                RejectionReason.POSITION_TOO_LARGE,
            ),
            _at_most(
                "open_positions",
                float(state.open_positions),
                float(limits.max_open_positions - 1),
                RejectionReason.TOO_MANY_OPEN_POSITIONS,
            ),
            _at_most(
                "open_for_token",
                float(state.open_for_token),
                float(limits.max_open_per_token - 1),
                RejectionReason.ALREADY_HOLDING_TOKEN,
            ),
            # Losses are negative, so the limit is a floor on realised PnL.
            _at_least(
                "daily_loss",
                state.realised_pnl_today_sol,
                -abs(limits.max_daily_loss_sol),
                RejectionReason.DAILY_LOSS_LIMIT,
            ),
            _at_most(
                "drawdown",
                state.drawdown_fraction,
                limits.max_drawdown_fraction,
                RejectionReason.DRAWDOWN_LIMIT,
                # No peak yet means no drawdown yet, not an unknown.
                none_passes=True,
            ),
            _at_least(
                "balance",
                state.balance_sol,
                requested_size_sol,
                RejectionReason.INSUFFICIENT_BALANCE,
            ),
        )


def _at_most(
    name: str,
    value: float | None,
    limit: float,
    reason: RejectionReason,
    *,
    none_passes: bool = False,
) -> RiskCheck:
    """Fails when the value is missing, unless the absence is itself meaningful.

    Unknown is not permission. A missing liquidity reading is not evidence of
    sufficient liquidity, and treating it as such is how a risk engine ends up
    approving exactly the trades it exists to stop.
    """
    passed = none_passes if value is None else value <= limit
    return RiskCheck(
        name=name, passed=passed, value=value, limit=limit, reason=None if passed else reason
    )


def _at_least(name: str, value: float | None, limit: float, reason: RejectionReason) -> RiskCheck:
    passed = value is not None and value >= limit
    return RiskCheck(
        name=name, passed=passed, value=value, limit=limit, reason=None if passed else reason
    )
