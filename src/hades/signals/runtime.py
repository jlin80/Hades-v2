"""Background lifecycle for signal research.

Same shape as the discovery and tracking runtimes: ``/status`` must be able to
tell "configured" from "actually running", and ``LoopSupervisor`` restarts the
loop rather than only recording why it died.
"""

from __future__ import annotations

import logging

from hades.config import Settings
from hades.db.engine import Database
from hades.features.engine import FeatureWindows
from hades.signals.early_momentum import EarlyMomentumConfig, EarlyMomentumStrategy
from hades.signals.notify import DiscordNotifier
from hades.signals.service import SignalService
from hades.supervision import LoopSupervisor

logger = logging.getLogger(__name__)


def build_strategy(settings: Settings) -> EarlyMomentumStrategy:
    """The one strategy, configured from settings.

    Every threshold is configurable because spec §12 requires the hypothesis to
    be configurable and not presented as truth. None of these values is derived
    from evidence yet — see the module docstring in ``early_momentum.py``.
    """
    return EarlyMomentumStrategy(
        EarlyMomentumConfig(
            window=settings.signal_window,
            min_token_age_seconds=settings.signal_min_token_age_seconds,
            max_token_age_seconds=settings.signal_max_token_age_seconds,
            min_market_cap_velocity=settings.signal_min_market_cap_velocity,
            min_market_cap_acceleration=settings.signal_min_market_cap_acceleration,
            min_liquidity_velocity=settings.signal_min_liquidity_velocity,
            min_price_movement_ratio=settings.signal_min_price_movement_ratio,
            max_seconds_since_last_trade=settings.signal_max_seconds_since_last_trade,
            min_liquidity_sol=settings.signal_min_liquidity_sol,
            min_observations=settings.signal_min_observations,
            max_freshness_seconds=settings.signal_max_freshness_seconds,
        )
    )


def build_signal_service(database: Database, settings: Settings) -> SignalService:
    notifier = (
        DiscordNotifier(settings.discord_webhook_url) if settings.discord_webhook_url else None
    )
    return SignalService(
        database,
        build_strategy(settings),
        windows=FeatureWindows(),
        series_lookback_seconds=settings.signal_series_lookback_seconds,
        batch_size=settings.signal_batch_size,
        pass_interval_seconds=settings.signal_pass_interval_seconds,
        observation_min_interval_seconds=settings.signal_observation_min_interval_seconds,
        notifier=notifier,
    )


class SignalRuntime:
    """Starts, stops and reports on the signal task."""

    def __init__(self, service: SignalService | None) -> None:
        self._service = service
        self._supervisor = LoopSupervisor("signals", service.run) if service is not None else None

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
    def strategy(self) -> str | None:
        return self._service.strategy_name if self._service else None

    @property
    def strategy_version(self) -> str | None:
        return self._service.strategy_version if self._service else None

    async def start(self) -> None:
        if self._supervisor is None:
            logger.info("signals_disabled")
            return
        self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        if self._service is not None:
            await self._service.aclose()
        logger.info("signals_stopped")
