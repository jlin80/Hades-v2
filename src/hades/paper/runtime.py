"""Background lifecycle for paper trading.

Same shape as the discovery, tracking and signal runtimes: ``/status`` must be
able to tell "configured" from "actually running", and a crashed loop must not
take the API down with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.paper.exits import ExitRules
from hades.paper.notify import PaperDiscordNotifier
from hades.paper.service import PaperConfig, PaperTradingService
from hades.risk.engine import RiskEngine, RiskLimits

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
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def counters(self) -> dict[str, int]:
        return self._service.counters.as_dict() if self._service else {}

    @property
    def service(self) -> PaperTradingService | None:
        """Exposed so ``/status`` can read live portfolio state on demand."""
        return self._service

    async def start(self) -> None:
        service = self._service
        if service is None:
            logger.info("paper_trading_disabled")
            return
        self._task = asyncio.create_task(self._supervise(service), name="paper-trading")

    async def _supervise(self, service: PaperTradingService) -> None:
        try:
            await service.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("paper_trading_crashed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._service is not None:
            await self._service.aclose()
        logger.info("paper_trading_stopped")
