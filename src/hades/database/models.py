"""ORM models.

Every model lives here so ``migrations/env.py`` has one module to import and
autogenerate can never silently miss a table.

All timestamps are ``TIMESTAMP WITH TIME ZONE`` and always UTC (task.md §14).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hades.database.base import Base

# Solana addresses are base58-encoded 32-byte values: 32-44 characters. The
# column is sized with headroom rather than to the exact bound.
ADDRESS_LENGTH = 64


class Token(Base):
    """A token we have discovered and will collect data about.

    One row per token address, forever. Rediscovering a token does not create a
    second row and does not modify the first: ``discovered_at`` records when we
    first saw it, which is a fact about our observation history and must not be
    rewritten by a later sighting (task.md §13).
    """

    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # The token's identity, and the idempotency key for discovery. The unique
    # constraint is what makes repeated processing of the same token safe.
    token_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH), nullable=False, unique=True)

    # NULL when the provider does not supply it. Never invented (task.md §7).
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))

    # When our system first observed this token.
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # When the token's pool was created, per the provider. This is the token's
    # real age; discovered_at is only our latency to noticing it.
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pool_address: Mapped[str | None] = mapped_column(String(ADDRESS_LENGTH))

    # Which provider produced the discovery, so a bad source can be traced.
    discovery_provider: Mapped[str] = mapped_column(String(64), nullable=False)

    # The provider's own object, verbatim. Kept so that an upstream schema
    # change can be detected after the fact instead of silently losing fields
    # we had not thought to parse.
    raw_provider_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Discovery reads "what did we see most recently" and Phase 4 will read
        # "what did we see in this window". Both are ordered scans over
        # discovered_at, so it is indexed on its own rather than as a trailing
        # column. v1 shipped an index that could not serve its actual query
        # pattern and paid 36 seconds a call for it.
        Index("ix_tokens_discovered_at", "discovered_at"),
    )

    def __repr__(self) -> str:
        return f"<Token {self.token_address} symbol={self.symbol!r}>"
