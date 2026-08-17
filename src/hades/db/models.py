"""ORM models.

Phase 0 defines exactly one table: ``tokens``. It exists now — ahead of the
discovery code that will fill it in Phase 2 — because ``/status`` must report
measured numbers, and a counter with no table behind it can only be a
fabricated zero.

Nothing else is modelled yet. Snapshots, features, signals and paper trades
arrive with the phase that produces them.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from hades.db.base import Base


class TokenState(enum.StrEnum):
    """Lifecycle of a token, as observed by us — not as claimed by a provider.

    ``CREATED`` is the on-chain fact; ``DISCOVERED`` is the moment we saw it.
    The two differ by our detection latency, and keeping them apart is what
    makes that latency measurable instead of invisible.
    """

    CREATED = "CREATED"
    DISCOVERED = "DISCOVERED"
    TRACKING = "TRACKING"
    ACTIVE = "ACTIVE"
    MIGRATED = "MIGRATED"
    INACTIVE = "INACTIVE"
    DEAD = "DEAD"


class Token(Base):
    """A Pump.fun token we have seen at least once.

    ``token_address`` is unique, which is what makes discovery idempotent: a
    restart re-observing the same mint updates a row instead of creating a
    second one.
    """

    __tablename__ = "tokens"
    __table_args__ = (
        Index("ix_tokens_state_discovered_at", "state", "discovered_at"),
        Index("ix_tokens_created_at", "created_at"),
    )

    # Generic Uuid, not postgresql.UUID: renders as native UUID on Postgres and
    # as CHAR(32) on SQLite, which lets the test suite exercise the real upsert
    # SQL without a database server. The migrations stay Postgres-specific.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_address: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # Provider-supplied metadata. Nullable on purpose: a missing symbol is a
    # missing symbol, never an empty string standing in for one.
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))
    creator_address: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[TokenState] = mapped_column(
        Enum(TokenState, name="token_state", native_enum=True, validate_strings=True),
        nullable=False,
        default=TokenState.DISCOVERED,
    )

    # On-chain proof of the claim: the creation tx signature, where the
    # discovering source supplied one. It is what lets any row in the research
    # dataset be verified against the chain independently of us.
    raw_provider_reference: Mapped[str | None] = mapped_column(String(128))

    # How many times we have asked the primary for this token's missing
    # created_at. Persisted rather than counted in memory: some mints never
    # appear in pump.fun's index at all (external launchpads), and without a
    # budget that survives restarts the backfill re-requests them forever and
    # starves the tokens whose indexing race simply has not resolved yet.
    backfill_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
