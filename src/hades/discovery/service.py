"""Discovery orchestration.

Two sources, one funnel:

* **PumpPortal WebSocket** pushes a mint the moment it is created. Fast, but
  carries no creation timestamp and loses everything sent while disconnected.
* **pump.fun** supplies the authoritative ``created_timestamp``, and polling it
  backfills whatever the socket missed.

Both paths go through the same idempotent upsert, so overlap between them costs
nothing and a gap in either is recoverable. That is the entire reason there are
two, and it is why neither needs to be reliable on its own.

There is no queue and no bus between the pieces (``docs/DECISIONS.md`` D1). A
frame becomes a row in the same task that received it, so there is nowhere for
a computed result to be dropped in transit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hades.db.engine import Database
from hades.discovery.repository import TokenRepository, UpsertOutcome
from hades.providers.errors import ProviderError, ProviderRateLimitedError
from hades.providers.models import DiscoveredToken
from hades.providers.pumpfun import PumpFunProvider
from hades.providers.pumpportal import PumpPortalProvider

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryCounters:
    """In-process counters. The database remains the source of truth."""

    ws_events: int = 0
    poll_items: int = 0
    inserted: int = 0
    enriched: int = 0
    unchanged: int = 0
    backfill_fetches: int = 0
    backfill_failures: int = 0
    provider_errors: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def as_dict(self) -> dict[str, int]:
        return {
            "ws_events": self.ws_events,
            "poll_items": self.poll_items,
            "inserted": self.inserted,
            "enriched": self.enriched,
            "unchanged": self.unchanged,
            "backfill_fetches": self.backfill_fetches,
            "backfill_failures": self.backfill_failures,
            "provider_errors": self.provider_errors,
        }


class DiscoveryService:
    """Runs the WebSocket listener and the polling backfill concurrently."""

    def __init__(
        self,
        database: Database,
        *,
        pumpfun: PumpFunProvider | None = None,
        pumpportal: PumpPortalProvider | None = None,
        poll_interval_seconds: float = 60.0,
        poll_limit: int = 50,
        backfill_limit: int = 50,
        backfill_max_attempts: int = 5,
    ) -> None:
        self._database = database
        self._pumpfun = pumpfun or PumpFunProvider()
        self._pumpportal = pumpportal or PumpPortalProvider()
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_limit = poll_limit
        self._backfill_limit = backfill_limit
        self._backfill_max_attempts = backfill_max_attempts
        self.counters = DiscoveryCounters()
        # Known mints, loaded from the database at start. Purely a rate-limit
        # saver: the unique constraint is what actually prevents duplicates.
        self._known: set[str] = set()

    async def recover(self) -> int:
        """Load what we already know. The 'recover after restart' step (§7)."""
        async with self._database.session() as session:
            self._known = await TokenRepository(session).recover_known_addresses()
        logger.info(
            "discovery_recovered",
            extra={"context": {"known_tokens": len(self._known)}},
        )
        return len(self._known)

    async def handle(self, token: DiscoveredToken) -> UpsertOutcome:
        """Persist one sighting. Nothing blocks the write.

        Note what is *not* here: a fetch to the primary for the missing
        ``created_at``. Doing it inline was the original design and it was
        measured to be wrong twice over — see ``backfill_created_at``.
        """
        async with self._database.session() as session:
            outcome = await TokenRepository(session).upsert(token)

        if outcome is UpsertOutcome.INSERTED:
            self.counters.inserted += 1
        elif outcome is UpsertOutcome.ENRICHED:
            self.counters.enriched += 1
        else:
            self.counters.unchanged += 1

        self._known.add(token.token_address)
        return outcome

    async def backfill_created_at(self) -> int:
        """Fill in creation timestamps for tokens that still lack one.

        Deferred rather than inline, for two reasons both found by running it:

        1. **The primary 404s on a mint the socket just announced.** Measured
           over a 75-second live run: 49 of 51 immediate fetches returned 404.
           The socket sees a creation before pump.fun's own API has indexed it.
           By the time a token reaches this pass it is seconds older and the
           race has resolved.
        2. **An inline fetch corrupted the measurement.** It sat between the
           frame arriving and the row being written, so its latency — mostly the
           latency of a 404 — was counted as detection latency.

        Each token gets a bounded number of attempts. Without that budget a mint
        that pump.fun never indexes — measured: some return 404 on every attempt,
        minutes apart — sits at the head of the queue forever and starves the
        tokens whose race simply has not resolved yet.

        Returns how many tokens were filled in.
        """
        async with self._database.session() as session:
            pending = await TokenRepository(session).addresses_missing_created_at(
                limit=self._backfill_limit, max_attempts=self._backfill_max_attempts
            )
        if not pending:
            return 0

        filled = 0
        for address in pending:
            self.counters.backfill_fetches += 1
            try:
                authoritative = await self._pumpfun.fetch_token(address)
            except ProviderRateLimitedError as exc:
                # Not the token's fault, so it does not spend a retry.
                self.counters.provider_errors += 1
                logger.warning(
                    "backfill_rate_limited",
                    extra={"context": {"retry_after_seconds": exc.retry_after_seconds}},
                )
                break
            except ProviderError as exc:
                # Still not indexed, or never will be. Leaving created_at NULL
                # is correct (spec §9); the budget decides how long we keep asking.
                self.counters.backfill_failures += 1
                await self._spend_attempt(address)
                logger.info(
                    "backfill_pending",
                    extra={"context": {"token_address": address, "reason": str(exc)}},
                )
                continue

            if authoritative.created_at is None:
                self.counters.backfill_failures += 1
                await self._spend_attempt(address)
                continue

            async with self._database.session() as session:
                outcome = await TokenRepository(session).upsert(authoritative)
            if outcome is UpsertOutcome.ENRICHED:
                filled += 1
                self.counters.enriched += 1

        logger.info(
            "backfill_complete",
            extra={"context": {"pending": len(pending), "filled": filled}},
        )
        return filled

    async def _spend_attempt(self, token_address: str) -> None:
        async with self._database.session() as session:
            await TokenRepository(session).record_backfill_attempt(token_address)

    async def run_websocket(self) -> None:
        """Consume the push stream until cancelled."""
        async for token in self._pumpportal.stream_new_tokens():
            self.counters.ws_events += 1
            try:
                await self.handle(token)
            except ProviderError as exc:
                self.counters.provider_errors += 1
                logger.warning("discovery_handle_failed", extra={"context": {"reason": str(exc)}})

    async def poll_once(self) -> int:
        """One backfill pass. Returns how many tokens were newly inserted."""
        try:
            tokens = await self._pumpfun.list_recent(limit=self._poll_limit)
        except ProviderRateLimitedError as exc:
            self.counters.provider_errors += 1
            wait = exc.retry_after_seconds or self._poll_interval_seconds
            logger.warning("poll_rate_limited", extra={"context": {"wait_seconds": wait}})
            await asyncio.sleep(wait)
            return 0
        except ProviderError as exc:
            self.counters.provider_errors += 1
            logger.warning("poll_failed", extra={"context": {"reason": str(exc)}})
            return 0

        self.counters.poll_items += len(tokens)
        inserted = 0
        for token in tokens:
            # These already carry created_at, so no enrichment round-trip.
            async with self._database.session() as session:
                outcome = await TokenRepository(session).upsert(token)
            if outcome is UpsertOutcome.INSERTED:
                self.counters.inserted += 1
                inserted += 1
            elif outcome is UpsertOutcome.ENRICHED:
                self.counters.enriched += 1
            else:
                self.counters.unchanged += 1
            self._known.add(token.token_address)

        logger.info(
            "poll_complete",
            extra={"context": {"items": len(tokens), "inserted": inserted}},
        )
        return inserted

    async def run_poller(self) -> None:
        """Poll for new tokens, then fill in missing timestamps. Until cancelled."""
        while True:
            await self.poll_once()
            await self.backfill_created_at()
            await asyncio.sleep(self._poll_interval_seconds)

    async def run(self) -> None:
        """Both loops together. Cancelling this cancels both."""
        await self.recover()
        async with asyncio.TaskGroup() as group:
            group.create_task(self.run_websocket(), name="discovery-websocket")
            group.create_task(self.run_poller(), name="discovery-poller")

    async def aclose(self) -> None:
        await self._pumpfun.aclose()
