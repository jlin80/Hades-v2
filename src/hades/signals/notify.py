"""Push a research signal to Discord. Optional, best-effort, never load-bearing.

A signal is already durable the moment ``SignalRepository.record_signal``
commits — this module only mirrors that fact somewhere a human will see it
faster than by querying Postgres. So a notification failure must never affect
the signal pipeline: no retry loop that could back up the signal service behind
Discord's rate limit, no exception that could reach the caller.

The message repeats the same disclaimer the API and the strategy already carry
in-process. A notification is the one surface a human reads without going
through `/status`, so it is also the one place a reader could mistake a signal
for an instruction if the disclaimer were left off.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from hades.signals.models import Signal

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "Research signal only. No order was placed. Hades V2 has no signer, no "
    "wallet and no transaction path — see docs/SAFETY.md. Whether this "
    "hypothesis has positive expectancy after fees and slippage is unmeasured; "
    "the first live read (Phase 7) was negative."
)


class DiscordNotifier:
    """Fire-and-forget POST to a Discord webhook."""

    def __init__(self, webhook_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def send_signal(self, signal: Signal, vector_values: dict[str, float | None]) -> None:
        """Best-effort. Logs and swallows any failure rather than raising.

        Never retried: a webhook outage must not stall the signal loop behind
        it, and a duplicate post on retry would be worse than a missed one —
        the database row is the record either way.
        """
        embed = _build_embed(signal, vector_values)
        try:
            response = await self._client.post(self._url, json={"embeds": [embed]})
            if response.status_code >= 400:
                logger.warning(
                    "discord_notify_failed",
                    extra={
                        "context": {
                            "status": response.status_code,
                            "token_address": signal.token_address,
                        }
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "discord_notify_error",
                extra={
                    "context": {
                        "reason": f"{type(exc).__name__}: {exc}",
                        "token_address": signal.token_address,
                    }
                },
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def _build_embed(signal: Signal, values: dict[str, float | None]) -> dict[str, object]:
    def fmt(name: str, digits: int = 4) -> str:
        value = values.get(name)
        return "—" if value is None else f"{value:.{digits}f}"

    fields = [
        {"name": "Token", "value": f"`{signal.token_address}`", "inline": False},
        {"name": "Age (s)", "value": fmt("token_age_seconds", 0), "inline": True},
        {"name": "Market cap (SOL)", "value": fmt("market_cap_sol", 2), "inline": True},
        {"name": "Liquidity (SOL)", "value": fmt("liquidity_sol", 2), "inline": True},
        {
            "name": "Mcap velocity (30s)",
            "value": fmt("market_cap_velocity_30s", 4),
            "inline": True,
        },
        {
            "name": "Mcap accel. (30s)",
            "value": fmt("market_cap_acceleration_30s", 4),
            "inline": True,
        },
        {
            "name": "Price movement ratio",
            "value": fmt("price_movement_ratio_30s", 2),
            "inline": True,
        },
        {
            "name": "Conditions",
            "value": "\n".join(f"{'✅' if c.passed else '❌'} {c.name}" for c in signal.conditions)
            or "—",
            "inline": False,
        },
    ]
    return {
        "title": f"Signal: {signal.strategy} v{signal.strategy_version}",
        "color": 0x5865F2,
        "timestamp": _isoformat(signal.created_at),
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


def _isoformat(value: datetime) -> str:
    return value.isoformat()
