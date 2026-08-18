"""The eight gates of spec §13, plus the one measurement made necessary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hades.risk.engine import (
    RejectionReason,
    RiskDecision,
    RiskEngine,
    RiskLimits,
    RiskState,
)

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)

HEALTHY = RiskState(
    balance_sol=1.0,
    open_positions=0,
    open_for_token=0,
    realised_pnl_today_sol=0.0,
    peak_equity_sol=1.0,
    current_equity_sol=1.0,
)


def decide(
    engine: RiskEngine | None = None,
    *,
    state: RiskState = HEALTHY,
    signal_age_seconds: float = 1.0,
    token_age_seconds: float | None = 60.0,
    liquidity_sol: float | None = 10.0,
    estimated_slippage: float | None = 0.01,
    requested_size_sol: float = 0.02,
) -> RiskDecision:
    return (engine or RiskEngine()).evaluate(
        signal_created_at=NOW - timedelta(seconds=signal_age_seconds),
        decision_at=NOW,
        token_age_seconds=token_age_seconds,
        liquidity_sol=liquidity_sol,
        estimated_slippage=estimated_slippage,
        state=state,
        requested_size_sol=requested_size_sol,
    )


class TestApproval:
    def test_a_clean_signal_is_approved(self) -> None:
        decision = decide()
        assert decision.approved
        assert decision.position_size_sol == 0.02
        assert decision.rejections == ()

    def test_the_decision_carries_the_timestamps_spec_13_requires(self) -> None:
        """signal_created_at, decision_at, data_age_ms -- all three, always."""
        decision = decide(signal_age_seconds=2.5)
        assert decision.signal_created_at == NOW - timedelta(seconds=2.5)
        assert decision.decision_at == NOW
        assert decision.data_age_ms == pytest.approx(2500.0)

    def test_size_is_capped_at_the_limit_not_rejected(self) -> None:
        """Asking for too much is a rejection; the cap applies to what is allowed."""
        engine = RiskEngine(RiskLimits(max_position_sol=0.05))
        assert decide(engine, requested_size_sol=0.5).approved is False
        assert decide(engine, requested_size_sol=0.05).position_size_sol == 0.05


class TestEachGate:
    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"signal_age_seconds": 120.0}, RejectionReason.STALE_SIGNAL),
            ({"token_age_seconds": 9999.0}, RejectionReason.TOKEN_TOO_OLD),
            ({"liquidity_sol": 0.1}, RejectionReason.INSUFFICIENT_LIQUIDITY),
            ({"estimated_slippage": 0.9}, RejectionReason.SLIPPAGE_TOO_HIGH),
            ({"requested_size_sol": 10.0}, RejectionReason.POSITION_TOO_LARGE),
        ],
    )
    def test_a_breached_limit_rejects_with_its_own_reason(
        self, kwargs: dict[str, float], reason: RejectionReason
    ) -> None:
        decision = decide(**kwargs)  # type: ignore[arg-type]
        assert not decision.approved
        assert reason in decision.rejections

    def test_stale_data_is_rejected_by_name(self) -> None:
        """Spec §13 names this one: never decide on obsolete information."""
        decision = decide(signal_age_seconds=60.0)
        assert decision.rejections == (RejectionReason.STALE_SIGNAL,)

    def test_too_many_open_positions(self) -> None:
        engine = RiskEngine(RiskLimits(max_open_positions=3))
        crowded = RiskState(
            balance_sol=1.0,
            open_positions=3,
            open_for_token=0,
            realised_pnl_today_sol=0.0,
            peak_equity_sol=1.0,
            current_equity_sol=1.0,
        )
        assert RejectionReason.TOO_MANY_OPEN_POSITIONS in decide(engine, state=crowded).rejections

    def test_daily_loss_limit_blocks_further_trading(self) -> None:
        engine = RiskEngine(RiskLimits(max_daily_loss_sol=0.5))
        bleeding = RiskState(
            balance_sol=1.0,
            open_positions=0,
            open_for_token=0,
            realised_pnl_today_sol=-0.6,
            peak_equity_sol=1.0,
            current_equity_sol=0.4,
        )
        assert RejectionReason.DAILY_LOSS_LIMIT in decide(engine, state=bleeding).rejections

    def test_drawdown_limit(self) -> None:
        engine = RiskEngine(RiskLimits(max_drawdown_fraction=0.25))
        drawn_down = RiskState(
            balance_sol=1.0,
            open_positions=0,
            open_for_token=0,
            realised_pnl_today_sol=0.0,
            peak_equity_sol=1.0,
            current_equity_sol=0.5,
        )
        assert RejectionReason.DRAWDOWN_LIMIT in decide(engine, state=drawn_down).rejections

    def test_insufficient_balance(self) -> None:
        broke = RiskState(
            balance_sol=0.001,
            open_positions=0,
            open_for_token=0,
            realised_pnl_today_sol=0.0,
            peak_equity_sol=1.0,
            current_equity_sol=0.001,
        )
        assert RejectionReason.INSUFFICIENT_BALANCE in decide(state=broke).rejections


class TestOnePositionPerToken:
    """Not in §13. Required by measurement.

    Phase 5's live run produced four signals on the same token inside a minute,
    because consecutive observations of a token in a momentum regime all satisfy
    the hypothesis. Correct for research, ruinous for position sizing.
    """

    def test_a_second_signal_on_a_held_token_is_rejected(self) -> None:
        already = RiskState(
            balance_sol=1.0,
            open_positions=1,
            open_for_token=1,
            realised_pnl_today_sol=0.0,
            peak_equity_sol=1.0,
            current_equity_sol=1.0,
        )
        assert RejectionReason.ALREADY_HOLDING_TOKEN in decide(state=already).rejections

    def test_a_different_token_is_still_allowed(self) -> None:
        elsewhere = RiskState(
            balance_sol=1.0,
            open_positions=1,
            open_for_token=0,
            realised_pnl_today_sol=0.0,
            peak_equity_sol=1.0,
            current_equity_sol=1.0,
        )
        assert decide(state=elsewhere).approved


class TestUnknownIsNotPermission:
    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"token_age_seconds": None}, RejectionReason.TOKEN_TOO_OLD),
            ({"liquidity_sol": None}, RejectionReason.INSUFFICIENT_LIQUIDITY),
            ({"estimated_slippage": None}, RejectionReason.SLIPPAGE_TOO_HIGH),
        ],
    )
    def test_a_missing_input_rejects(
        self, kwargs: dict[str, float | None], reason: RejectionReason
    ) -> None:
        """A missing liquidity reading is not evidence of sufficient liquidity.

        Treating unknown as permission is how a risk engine ends up approving
        exactly the trades it exists to stop.
        """
        decision = decide(**kwargs)  # type: ignore[arg-type]
        assert not decision.approved
        assert reason in decision.rejections

    def test_no_peak_equity_yet_is_not_a_drawdown(self) -> None:
        """The one absence that genuinely means 'fine'.

        A portfolio that has never had a peak has not fallen from one.
        """
        fresh = RiskState(
            balance_sol=1.0,
            open_positions=0,
            open_for_token=0,
            realised_pnl_today_sol=0.0,
            peak_equity_sol=0.0,
            current_equity_sol=1.0,
        )
        assert fresh.drawdown_fraction is None
        assert decide(state=fresh).approved


class TestFailClosed:
    def test_an_exception_becomes_a_rejection_not_an_escape(self) -> None:
        """The single most valuable line in a risk engine.

        A bug here must block trading, not permit it.
        """

        class Exploding(RiskEngine):
            def _checks(self, **kwargs: object) -> tuple:  # type: ignore[type-arg]
                msg = "boom"
                raise RuntimeError(msg)

        decision = decide(Exploding())
        assert not decision.approved
        assert decision.position_size_sol == 0.0
        assert RejectionReason.ENGINE_ERROR in decision.rejections

    def test_a_rejected_decision_always_sizes_at_zero(self) -> None:
        assert decide(liquidity_sol=0.0).position_size_sol == 0.0


class TestExplainability:
    def test_every_gate_is_recorded_pass_or_fail(self) -> None:
        decision = decide(liquidity_sol=0.0, estimated_slippage=0.9)
        assert len(decision.checks) == 10
        assert set(decision.rejections) == {
            RejectionReason.INSUFFICIENT_LIQUIDITY,
            RejectionReason.SLIPPAGE_TOO_HIGH,
        }

    def test_checks_serialise_with_value_and_limit(self) -> None:
        payload = decide(liquidity_sol=0.1).as_dict()
        assert payload["approved"] is False
        checks = payload["checks"]
        assert isinstance(checks, list)
        liquidity = next(c for c in checks if c["name"] == "liquidity")
        assert liquidity["value"] == 0.1
        assert liquidity["limit"] == 1.0
        assert liquidity["reason"] == "INSUFFICIENT_LIQUIDITY"
