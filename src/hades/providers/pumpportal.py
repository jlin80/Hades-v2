"""DISCOVERY provider — PumpPortal WebSocket.

Push-based token creation events: free, keyless, and measured in Phase 1 at
~0.24 creations/s (~21k/day), cross-checked against the primary.

Why push rather than polling: the gap between a token being created and us
observing it is the one latency this system cannot design around — every
early-window feature is computed from it. Polling puts a floor under that gap
equal to the poll interval. This removes the floor.

The creation event carries **no timestamp**. ``created_at`` is therefore None
here and gets filled from the primary. Stamping arrival time as creation time
would zero out exactly the latency we want to measure.

``subscribeTokenTrade`` is not used: it requires an API key funded with 0.02 SOL
(measured — the server says so). See ``docs/DATA_SOURCES.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import websockets
from websockets.exceptions import WebSocketException

from hades.providers.models import DiscoveredToken

logger = logging.getLogger(__name__)

SOURCE = "pumpportal"
WS_URL = "wss://pumpportal.fun/api/data"


class PumpPortalProvider:
    """Streams token creation events, reconnecting with backoff."""

    def __init__(
        self,
        *,
        url: str = WS_URL,
        open_timeout_seconds: float = 15.0,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = 60.0,
    ) -> None:
        self.url = url
        self.open_timeout_seconds = open_timeout_seconds
        self.reconnect_base_seconds = reconnect_base_seconds
        self.reconnect_max_seconds = reconnect_max_seconds

    async def stream_new_tokens(self) -> AsyncGenerator[DiscoveredToken]:
        """Yield created tokens until cancelled.

        Reconnects forever. A disconnect loses whatever was created while we
        were away, which is why the primary's ``list_recent`` backfill exists —
        the two together are what make a gap recoverable rather than permanent.
        """
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    self.url, open_timeout=self.open_timeout_seconds
                ) as socket:
                    await socket.send(json.dumps({"method": "subscribeNewToken"}))
                    logger.info("ws_subscribed", extra={"context": {"provider": SOURCE}})
                    attempt = 0

                    async for raw in socket:
                        token = self._parse(raw)
                        if token is not None:
                            yield token
            except asyncio.CancelledError:
                raise
            except (WebSocketException, OSError, TimeoutError) as exc:
                attempt += 1
                delay = min(
                    self.reconnect_base_seconds * (2 ** (attempt - 1)),
                    self.reconnect_max_seconds,
                )
                logger.warning(
                    "ws_reconnecting",
                    extra={
                        "context": {
                            "provider": SOURCE,
                            "attempt": attempt,
                            "delay_seconds": delay,
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    },
                )
                await asyncio.sleep(delay)

    def _parse(self, raw: str | bytes) -> DiscoveredToken | None:
        """Map one frame to a DiscoveredToken, or None if it is not one.

        Non-token frames are normal traffic here: the server sends subscription
        acknowledgements and gate messages on the same socket.
        """
        try:
            frame: Any = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(
                "ws_frame_not_json",
                extra={"context": {"provider": SOURCE, "frame": str(raw)[:200]}},
            )
            return None

        if not isinstance(frame, dict):
            return None

        if "message" in frame and "mint" not in frame:
            # Server-side notice, e.g. the funded-key gate on trade streams.
            logger.info(
                "ws_server_message",
                extra={"context": {"provider": SOURCE, "message": str(frame["message"])[:300]}},
            )
            return None

        if frame.get("txType") not in (None, "create"):
            return None

        mint = frame.get("mint")
        if not isinstance(mint, str):
            return None

        try:
            return DiscoveredToken(
                token_address=mint,
                symbol=_as_str(frame.get("symbol")),
                name=_as_str(frame.get("name")),
                # On a create event this is the creator's wallet.
                creator_address=_as_str(frame.get("traderPublicKey")),
                created_at=None,
                source=SOURCE,
                raw_provider_reference=_as_str(frame.get("signature")),
            )
        except ValueError as exc:
            logger.warning(
                "ws_frame_invalid",
                extra={"context": {"provider": SOURCE, "mint": mint, "reason": str(exc)}},
            )
            return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


async def collect_new_tokens(
    provider: PumpPortalProvider, *, seconds: float, limit: int = 100
) -> list[DiscoveredToken]:
    """Collect events for a bounded window. Used by tests and by the probes."""
    collected: list[DiscoveredToken] = []
    stream = provider.stream_new_tokens()

    async def drain() -> None:
        async for token in stream:
            collected.append(token)
            if len(collected) >= limit:
                return

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(drain(), timeout=seconds)
    await stream.aclose()
    return collected
