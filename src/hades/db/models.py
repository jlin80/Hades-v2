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

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
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

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # How many times we have asked the primary for this token's missing
    # created_at. Persisted rather than counted in memory: some mints never
    # appear in pump.fun's index at all (external launchpads), and without a
    # budget that survives restarts the backfill re-requests them forever and
    # starves the tokens whose indexing race simply has not resolved yet.
    backfill_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    # --- Tracking (Phase 3) --------------------------------------------------
    # When this token was admitted to tracking, and when it is next due. The
    # due time is persisted rather than recomputed at boot so that a restart
    # resumes a token's series instead of restarting its schedule.
    tracking_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    # Consecutive failures. A token whose provider record has disappeared stops
    # being polled rather than consuming a slot forever.
    snapshot_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )


class MarketSnapshot(Base):
    """One observation of a token's market state, at one instant.

    Append-only. A snapshot is a measurement, and a measurement that can be
    edited is not evidence — spec §11 needs the values behind a decision to
    stay exactly as they were.

    The raw curve reserves are the primary record; price, market cap and
    liquidity are stored alongside because queries need them, but they are
    reproducible from the raw fields via ``hades.tracking.derive``. That is what
    makes a formula error found in six months fixable against data already
    collected rather than fatal to it.
    """

    __tablename__ = "market_snapshots"
    __table_args__ = (
        # The query every feature computation makes: one token's series in
        # time order.
        Index("ix_market_snapshots_token_observed", "token_id", "observed_at"),
        Index("ix_market_snapshots_observed_at", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
    )
    token_address: Mapped[str] = mapped_column(String(64), nullable=False)

    # Provenance (spec §9). Three clocks, three different questions: how stale
    # the provider said its own data was, when we observed it, when it landed.
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    token_age_seconds: Mapped[float | None] = mapped_column(Float)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)

    # Raw bonding-curve state, in base units exactly as reported. BigInteger:
    # total_supply is 1e15 and overflows a 32-bit column.
    virtual_sol_reserves: Mapped[int | None] = mapped_column(BigInteger)
    virtual_token_reserves: Mapped[int | None] = mapped_column(BigInteger)
    real_sol_reserves: Mapped[int | None] = mapped_column(BigInteger)
    real_token_reserves: Mapped[int | None] = mapped_column(BigInteger)
    total_supply: Mapped[int | None] = mapped_column(BigInteger)
    base_decimals: Mapped[int | None] = mapped_column(Integer)
    quote_decimals: Mapped[int | None] = mapped_column(Integer)

    # Derived, reproducible from the above.
    price_sol: Mapped[float | None] = mapped_column(Float)
    market_cap_sol: Mapped[float | None] = mapped_column(Float)
    liquidity_sol: Mapped[float | None] = mapped_column(Float)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)
    sol_price_usd: Mapped[float | None] = mapped_column(Float)

    is_complete: Mapped[bool | None] = mapped_column(Boolean)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_count: Mapped[int | None] = mapped_column(Integer)

    # How old the provider said its own record was when we read it, and whether
    # that crossed the configured threshold. Stored rather than recomputed, so a
    # later change to the threshold cannot rewrite history.
    #
    # ⚠️ Read these carefully. On pump.fun, `updated_at` tracks the last time the
    # record *changed*, which is essentially the last trade — measured 1 second
    # apart on a live token. So for this provider these fields are dominated by
    # **trade inactivity**, not by provider lag: a token nobody is trading
    # reports a growing data age while the data itself is perfectly accurate.
    #
    # That makes `is_stale` an activity signal here, not a freshness one. Real
    # freshness gating (spec §13, reject a decision made on old data) needs a
    # decision to be timed against a snapshot, and that belongs to Phase 6.
    # `seconds_since_last_trade` is derivable from `last_trade_at` above and is
    # the unambiguous way to express what this actually measures.
    provider_data_age_seconds: Mapped[float | None] = mapped_column(Float)
    is_stale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    # Deliberately no updated_at: a snapshot is a measurement at an instant and
    # is never modified after it is written.
