"""Token persistence.

Spec §7: discovery must be idempotent, and a restart must never produce
duplicates. That guarantee lives here, in one SQL statement, rather than in an
in-memory "seen" set — a set is empty after a restart, which is precisely the
moment the guarantee is needed.

Hades V1 deduplicated in a `seen_registry` with a 24-hour TTL. It worked, and it
also meant the process could not answer "have we seen this?" without its own
memory being intact. The unique constraint on ``token_address`` answers it from
the database, which is the thing that survives.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy import or_ as sa_or
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from hades.db.models import Token, TokenState
from hades.providers.models import DiscoveredToken

logger = logging.getLogger(__name__)


class UpsertOutcome(enum.StrEnum):
    """What actually happened to a row. Reported, never guessed."""

    INSERTED = "INSERTED"
    ENRICHED = "ENRICHED"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class DiscoveryStats:
    """Measured counts from the database, for ``/status``."""

    total: int
    by_state: dict[str, int]
    with_created_at: int
    backfill_exhausted: int
    last_discovered_at: datetime | None
    median_discovery_latency_ms: float | None


class TokenRepository:
    """Reads and writes ``tokens``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, token: DiscoveredToken) -> UpsertOutcome:
        """Insert a token, or enrich the existing row without losing history.

        The conflict branch is where the care is. Three things must never be
        overwritten:

        * ``discovered_at`` — the first sighting is the fact we measure latency
          against. Re-observing a token does not make it newer.
        * ``source`` — attribution belongs to whoever saw it first.
        * a known value replaced by NULL — the WebSocket path has no
          ``created_at``, and it must not erase one the primary already supplied.

        And ``state`` only ever moves forward: a re-discovery must not drag a
        TRACKING token back to DISCOVERED.
        """
        dialect = self._session.bind.dialect.name if self._session.bind else "postgresql"
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert

        # discovered_at comes from the provider's own observation time, not
        # from now: anything we do between receiving a frame and writing it —
        # an enrichment fetch, a slow pool — would otherwise be counted as
        # detection latency. Measured: that inflated every reading by ~2.5s.
        now = token.observed_at

        statement = insert(Token).values(
            token_address=token.token_address,
            symbol=token.symbol,
            name=token.name,
            creator_address=token.creator_address,
            created_at=token.created_at,
            source=token.source,
            state=TokenState.DISCOVERED,
            raw_provider_reference=token.raw_provider_reference,
            discovered_at=now,
            updated_at=now,
        )
        excluded = statement.excluded

        # Columns a later sighting is allowed to fill in. Note what is absent:
        # discovered_at, source and state. First sighting, first attribution,
        # and no backward state transition.
        enrichable = ("symbol", "name", "creator_address", "created_at", "raw_provider_reference")

        upsert = statement.on_conflict_do_update(
            index_elements=[Token.token_address],
            # COALESCE(existing, incoming): fill gaps, never blank out what we
            # already know. The WebSocket path has no created_at and must not
            # erase one the primary already supplied.
            set_={
                column: func.coalesce(getattr(Token, column), getattr(excluded, column))
                for column in enrichable
            }
            | {"updated_at": now},
            # Only fire when this sighting actually adds something. Without
            # this, every re-observation would bump updated_at and the caller
            # could never tell a real enrichment from a no-op.
            where=sa_or(
                *(
                    getattr(Token, column).is_(None) & getattr(excluded, column).is_not(None)
                    for column in enrichable
                )
            ),
        ).returning(Token.discovered_at, Token.updated_at)

        row = (await self._session.execute(upsert)).first()
        await self._session.commit()
        # The statement went to the database directly, so any Token already in
        # this session's identity map still holds pre-upsert values. Without
        # this, a caller that reads back after writing gets stale data — and
        # gets it silently, which is the worst kind.
        self._session.expire_all()

        if row is None:
            # Conflict, and the WHERE rejected the update: we already knew
            # everything this sighting had to offer.
            return UpsertOutcome.UNCHANGED

        discovered_at, updated_at = row
        # An insert wrote `now` into both. A conflict update left the original
        # discovered_at in place and moved updated_at to `now`.
        if _as_utc(discovered_at) == _as_utc(updated_at):
            return UpsertOutcome.INSERTED
        return UpsertOutcome.ENRICHED

    async def get(self, token_address: str) -> Token | None:
        result: Token | None = await self._session.scalar(
            select(Token).where(Token.token_address == token_address)
        )
        return result

    async def count(self) -> int:
        return await self._session.scalar(select(func.count()).select_from(Token)) or 0

    async def count_by_state(self, state: TokenState) -> int:
        return (
            await self._session.scalar(
                select(func.count()).select_from(Token).where(Token.state == state)
            )
            or 0
        )

    async def recover_known_addresses(self, *, limit: int = 100_000) -> set[str]:
        """Every mint we have ever recorded.

        This is the "recover after restart" step: the process starts with no
        memory, asks the database what it already knows, and continues. It is
        an optimisation, not a correctness requirement — the unique constraint
        would catch a duplicate anyway. It exists to avoid spending the
        primary's rate limit re-enriching tokens we already have.
        """
        rows = await self._session.scalars(select(Token.token_address).limit(limit))
        return set(rows)

    async def addresses_missing_created_at(
        self, *, limit: int = 50, max_attempts: int = 5
    ) -> list[str]:
        """Tokens we know about, have no creation timestamp for, and may retry.

        Oldest sighting first: a mint that has been waiting is the one whose
        indexing race has definitely resolved, and the one whose token_age is
        blocking a tracking decision.

        ``max_attempts`` is what keeps a permanently-unindexed mint from
        occupying the queue forever. Measured: some tokens 404 on every attempt,
        minutes apart — most likely external-launchpad mints that reach the
        WebSocket but never enter pump.fun's own index.
        """
        rows = await self._session.scalars(
            select(Token.token_address)
            .where(Token.created_at.is_(None), Token.backfill_attempts < max_attempts)
            .order_by(Token.discovered_at.asc())
            .limit(limit)
        )
        return list(rows)

    async def record_backfill_attempt(self, token_address: str) -> None:
        """Count one attempt against a token's budget.

        Incremented in the database, not in memory: a restart must not hand a
        hopeless token a fresh budget, or the starvation this prevents simply
        returns on the next deploy.
        """
        await self._session.execute(
            update(Token)
            .where(Token.token_address == token_address)
            .values(backfill_attempts=Token.backfill_attempts + 1)
        )
        await self._session.commit()

    async def count_backfill_exhausted(self, *, max_attempts: int = 5) -> int:
        """Tokens that will never get a created_at from the primary."""
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(Token)
                .where(Token.created_at.is_(None), Token.backfill_attempts >= max_attempts)
            )
            or 0
        )

    async def stats(self, *, backfill_max_attempts: int = 5) -> DiscoveryStats:
        """Everything ``/status`` reports about discovery, measured on demand."""
        total = await self.count()
        by_state: dict[str, int] = {}
        for state in TokenState:
            count = await self.count_by_state(state)
            if count:
                by_state[state.value] = count

        with_created_at = (
            await self._session.scalar(
                select(func.count()).select_from(Token).where(Token.created_at.is_not(None))
            )
            or 0
        )
        last_discovered_at = await self._session.scalar(select(func.max(Token.discovered_at)))

        return DiscoveryStats(
            total=total,
            by_state=by_state,
            with_created_at=with_created_at,
            backfill_exhausted=await self.count_backfill_exhausted(
                max_attempts=backfill_max_attempts
            ),
            last_discovered_at=_as_utc_or_none(last_discovered_at),
            median_discovery_latency_ms=await self._median_discovery_latency_ms(),
        )

    async def _median_discovery_latency_ms(self) -> float | None:
        """How far behind creation our first sighting is.

        The whole point of keeping CREATED and DISCOVERED as separate columns
        (spec §7). Computed in Python over a bounded sample rather than in SQL,
        because percentile syntax differs across dialects and this is not a hot
        path.
        """
        rows = (
            await self._session.execute(
                select(Token.created_at, Token.discovered_at)
                .where(Token.created_at.is_not(None))
                .order_by(Token.discovered_at.desc())
                .limit(500)
            )
        ).all()

        deltas = sorted(
            (_as_utc(discovered) - _as_utc(created)).total_seconds() * 1000
            for created, discovered in rows
            if created is not None
        )
        if not deltas:
            return None
        middle = len(deltas) // 2
        if len(deltas) % 2:
            return round(deltas[middle], 1)
        return round((deltas[middle - 1] + deltas[middle]) / 2, 1)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)
