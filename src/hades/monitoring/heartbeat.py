"""Hourly Discord status heartbeat.

Independent of the trade notifications in ``hades.paper.notify``: those fire on
events, this fires on a clock, so the operator knows the process is alive even
during a quiet hour with no fills at all. Same best-effort contract — nothing
here is load-bearing, so a failed post is logged and dropped, never retried.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from hades.config import Settings
from hades.db.engine import Database
from hades.discovery.repository import TokenRepository
from hades.outcomes.service import OutcomeService
from hades.paper.service import PaperTradingService
from hades.signals.notify import DISCLAIMER
from hades.signals.repository import SignalRepository
from hades.supervision import LoopSupervisor
from hades.tracking.repository import TrackingRepository
from hades.tracking.runtime import schedule_from_settings

logger = logging.getLogger(__name__)

_COLOR_STATUS = 0x5865F2
_COLOR_RESEARCH_READY = 0xFEE75C


class DiscordHeartbeat:
    """A single best-effort POST describing the bot's current overall state."""

    def __init__(self, webhook_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def send_status(
        self,
        *,
        tokens_discovered: int,
        tokens_tracking: int,
        observations_total: int,
        signals_total: int,
        balance_sol: float,
        equity_sol: float,
        open_positions: int,
        trades_total: int,
    ) -> None:
        fields = [
            {"name": "Balance", "value": f"{balance_sol:.4f} SOL", "inline": True},
            {"name": "Equity", "value": f"{equity_sol:.4f} SOL", "inline": True},
            {"name": "Open positions", "value": str(open_positions), "inline": True},
            {"name": "Tokens discovered", "value": str(tokens_discovered), "inline": True},
            {"name": "Tokens tracking", "value": str(tokens_tracking), "inline": True},
            {
                "name": "Pipeline totals",
                "value": (
                    f"{observations_total} analyses -> {signals_total} signals -> "
                    f"{trades_total} trades"
                ),
                "inline": False,
            },
        ]
        await self._post(
            {
                "embeds": [
                    {
                        "title": "🩺 Hades V2 status",
                        "color": _COLOR_STATUS,
                        "fields": fields,
                        "footer": {"text": DISCLAIMER},
                    }
                ]
            }
        )

    async def send_research_ready(self, *, signalled_final_count: int, threshold: int) -> None:
        """One-off ping: enough finalised, signalled outcomes to run the report.

        @everyone because this is the one event in the whole system worth
        interrupting someone for -- every other notification here is
        best-effort and easy to miss on purpose.
        """
        embed = {
            "title": "🔬 Suficientes datos para evaluar EARLY MOMENTUM",
            "color": _COLOR_RESEARCH_READY,
            "fields": [
                {
                    "name": "Señales con outcome final",
                    "value": str(signalled_final_count),
                    "inline": True,
                },
                {"name": "Umbral configurado", "value": str(threshold), "inline": True},
                {
                    "name": "Próximo paso",
                    "value": "`python scripts/research_report.py <database> <label_config>`",
                    "inline": False,
                },
            ],
            "footer": {"text": DISCLAIMER},
        }
        await self._post(
            {
                "content": "@everyone",
                "embeds": [embed],
                "allowed_mentions": {"parse": ["everyone"]},
            }
        )

    async def _post(self, payload: dict[str, object]) -> None:
        try:
            response = await self._client.post(self._url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "discord_heartbeat_failed",
                    extra={"context": {"status": response.status_code}},
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "discord_heartbeat_error",
                extra={"context": {"reason": f"{type(exc).__name__}: {exc}"}},
            )

    async def aclose(self) -> None:
        await self._client.aclose()


class HeartbeatService:
    """Gathers cross-subsystem stats and posts them on a fixed interval."""

    def __init__(
        self,
        database: Database,
        notifier: DiscordHeartbeat,
        *,
        settings: Settings,
        paper_service: PaperTradingService | None,
        outcome_service: OutcomeService | None,
        interval_seconds: float,
    ) -> None:
        self._database = database
        self._notifier = notifier
        self._settings = settings
        self._paper_service = paper_service
        self._outcome_service = outcome_service
        self._interval_seconds = interval_seconds
        # In-memory, not persisted: a restart while still above threshold
        # re-arms the check and pings once more. Preferred over a DB flag --
        # missing the alert entirely is worse than one duplicate ping, and a
        # restart while waiting on this is rare.
        self._research_ready_announced = False

    async def _report_once(self) -> None:
        schedule = schedule_from_settings(self._settings)
        async with self._database.session() as session:
            discovery_stats = await TokenRepository(session).stats(
                backfill_max_attempts=self._settings.discovery_backfill_max_attempts
            )
            tracking_stats = await TrackingRepository(session, schedule).stats()
            signal_stats = await SignalRepository(session).stats()

        if self._paper_service is not None:
            state = await self._paper_service.portfolio()
            balance_sol = state.balance_sol
            equity_sol = state.current_equity_sol
            open_positions = state.open_positions
            trades_total = self._paper_service.counters.filled
        else:
            balance_sol = equity_sol = 0.0
            open_positions = trades_total = 0

        await self._notifier.send_status(
            tokens_discovered=discovery_stats.total,
            tokens_tracking=tracking_stats.tracking_now,
            observations_total=signal_stats.observations_total,
            signals_total=signal_stats.signals_total,
            balance_sol=balance_sol,
            equity_sol=equity_sol,
            open_positions=open_positions,
            trades_total=trades_total,
        )

        if self._outcome_service is not None and not self._research_ready_announced:
            threshold = self._settings.research_ready_threshold
            count = await self._outcome_service.signalled_final_count()
            if count >= threshold:
                await self._notifier.send_research_ready(
                    signalled_final_count=count, threshold=threshold
                )
                self._research_ready_announced = True

    async def run(self) -> None:
        while True:
            try:
                await self._report_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("heartbeat_report_failed")
            await asyncio.sleep(self._interval_seconds)

    async def aclose(self) -> None:
        await self._notifier.aclose()


class HeartbeatRuntime:
    """Starts, stops and reports on the heartbeat task."""

    def __init__(self, service: HeartbeatService | None) -> None:
        self._service = service
        self._supervisor = (
            LoopSupervisor("discord-heartbeat", service.run) if service is not None else None
        )

    @property
    def is_running(self) -> bool:
        return self._supervisor is not None and self._supervisor.is_running

    @property
    def last_error(self) -> str | None:
        return self._supervisor.last_error if self._supervisor else None

    async def start(self) -> None:
        if self._supervisor is None:
            logger.info("heartbeat_disabled")
            return
        self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        if self._service is not None:
            await self._service.aclose()
        logger.info("heartbeat_stopped")


def build_heartbeat_service(
    database: Database,
    settings: Settings,
    paper_service: PaperTradingService | None,
    outcome_service: OutcomeService | None,
) -> HeartbeatService | None:
    if not settings.discord_webhook_url or settings.discord_status_interval_seconds <= 0:
        return None
    return HeartbeatService(
        database,
        DiscordHeartbeat(settings.discord_webhook_url),
        settings=settings,
        paper_service=paper_service,
        outcome_service=outcome_service,
        interval_seconds=settings.discord_status_interval_seconds,
    )
