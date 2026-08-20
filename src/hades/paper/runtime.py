"""Background lifecycle for paper trading.

Same shape as the discovery, tracking and signal runtimes: ``/status`` must be
able to tell "configured" from "actually running", a crashed loop must not take
the API down with it, and ``LoopSupervisor`` brings it back afterwards.
"""

from __future__ import annotations

import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.paper.exits import ExitRules
from hades.paper.notify import PaperDiscordNotifier
from hades.paper.service import PaperConfig, PaperTradingService
from hades.risk.engine import RiskEngine, RiskLimits
from hades.supervision import LoopSupervisor

logger = logging.getLogger(__name__)


def build_risk_limits(settings: Settings) -> RiskLimits:
    """Spec §13's limits, all configurable and none validated by evidence yet."""
    return RiskLimits(
        max_token_age_seconds=settings.risk_max_token_age_seconds,
        min_liquidity_sol=settings.risk_min_liquidity_sol,
        max_slippage_fraction=settings.risk_max_slippage_fraction,
        max_position_sol=settings.risk_max_position_sol,
        max_open_positions=settings.risk_max_open_positions,
        max_open_per_token=settings.risk_max_open_per_token,
        max_daily_loss_sol=settings.risk_max_daily_loss_sol,
        max_drawdown_fraction=settings.risk_max_drawdown_fraction,
        max_data_age_seconds=settings.risk_max_data_age_seconds,
    )


def build_exit_rules(settings: Settings) -> ExitRules:
    return ExitRules(
        take_profit_fraction=settings.exit_take_profit_fraction,
        stop_loss_fraction=settings.exit_stop_loss_fraction,
        trailing_stop_fraction=settings.exit_trailing_stop_fraction,
        trailing_arm_fraction=settings.exit_trailing_arm_fraction,
        max_hold_seconds=settings.exit_max_hold_seconds,
    )


def build_paper_service(database: Database, settings: Settings) -> PaperTradingService:
    notifier = (
        PaperDiscordNotifier(settings.discord_webhook_url) if settings.discord_webhook_url else None
    )
    return PaperTradingService(
        database,
        risk=RiskEngine(build_risk_limits(settings)),
        exit_rules=build_exit_rules(settings),
        config=PaperConfig(
            starting_balance_sol=settings.paper_starting_balance_sol,
            position_size_sol=settings.paper_position_size_sol,
            fee_rate=settings.paper_fee_rate,
            latency_seconds=settings.paper_latency_seconds,
            pass_interval_seconds=settings.paper_pass_interval_seconds,
            batch_size=settings.paper_batch_size,
        ),
        notifier=notifier,
    )


class PaperRuntime:
    """Starts, stops and reports on the paper-trading task."""

    def __init__(self, service: PaperTradingService | None) -> None:
        self._service = service
        self._supervisor = (
            LoopSupervisor("paper-trading", service.run) if service is not None else None
        )

    @property
    def is_running(self) -> bool:
        return self._supervisor is not None and self._supervisor.is_running

    @property
    def last_error(self) -> str | None:
        return self._supervisor.last_error if self._supervisor else None

    @property
    def supervision(self) -> dict[str, object]:
        return self._supervisor.status() if self._supervisor else {}

    @property
    def counters(self) -> dict[str, int]:
        return self._service.counters.as_dict() if self._service else {}

    @property
    def service(self) -> PaperTradingService | None:
        """Exposed so ``/status`` can read live portfolio state on demand."""
        return self._service

    async def start(self) -> None:
        if self._supervisor is None:
            logger.info("paper_trading_disabled")
            return
        self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        if self._service is not None:
            await self._service.aclose()
        logger.info("paper_trading_stopped")
