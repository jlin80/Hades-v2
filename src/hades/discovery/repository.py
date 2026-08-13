"""Persistence for discovered tokens.

Idempotency is enforced by the database, not by application-level checking
(task.md §13). A "does it exist?" query followed by an insert is a race; a
unique constraint plus ``ON CONFLICT DO NOTHING`` is not.

``DO NOTHING`` rather than ``DO UPDATE`` is deliberate. ``discovered_at``
records when *we* first saw a token. A later sighting is not new information
about that fact, and overwriting it would quietly destroy the latency data
Phase 4 needs.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hades.database.models import Token
from hades.discovery.models import DiscoveredToken


async def insert_new_tokens(session: AsyncSession, tokens: list[DiscoveredToken]) -> int:
    """Insert tokens that are not already known and return how many were new.

    Existing rows are left completely untouched.
    """
    if not tokens:
        return 0

    rows = [
        {
            "token_address": token.token_address,
            "symbol": token.symbol,
            "name": token.name,
            "discovered_at": token.observed_at,
            "first_seen_at": token.first_seen_at,
            "pool_address": token.pool_address,
            "discovery_provider": token.provider_name,
            "raw_provider_data": token.raw,
        }
        for token in tokens
    ]

    statement = (
        insert(Token)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[Token.token_address])
        .returning(Token.token_address)
    )

    result = await session.execute(statement)
    inserted = len(result.scalars().all())
    await session.commit()
    return inserted


async def count_tokens(session: AsyncSession) -> int:
    """Return the total number of tokens discovered so far."""
    result = await session.execute(select(func.count()).select_from(Token))
    return int(result.scalar_one())


async def latest_discovery_at(session: AsyncSession) -> datetime | None:
    """Return when the most recent token was discovered, or None if none are.

    Read from the database rather than from process memory so the answer
    survives a restart (task.md §12).
    """
    result = await session.execute(select(func.max(Token.discovered_at)))
    return result.scalar_one_or_none()
