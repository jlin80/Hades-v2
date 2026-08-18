"""ORM models.

Four tables, each arriving with the phase that produces it:

* ``tokens`` (Phase 0/2) — one row per mint we have ever seen, unique on the
  address, which is what makes discovery idempotent across restarts.
* ``market_snapshots`` (Phase 3) — append-only observations of curve state.
* ``feature_observations`` (Phase 5) — the immutable feature vector at an
  instant, which spec §11 requires and §16 uses as the research dataset.
* ``signals`` (Phase 5) — research signals, pointing at the observation they
  were computed from.

Paper trades and outcomes arrive with Phases 6 and 7.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from hades.db.base import Base

# JSONB on Postgres, plain JSON on SQLite. The variant matters: §17's questions
# slice the dataset by feature value, and JSONB is indexable where a text blob
# is not — while the tests still need a dialect that has no JSONB at all.
_JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")


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


class FeatureObservation(Base):
    """The immutable feature vector at one instant (spec §11).

    Append-only, and never updated. §11 requires that the features used to make
    a decision stay intact — so this table has no mutable column at all, not
    even a timestamp, and nothing in the codebase issues an UPDATE against it.

    ``feature_version`` is what makes an old row still readable: a vector's
    meaning is fixed by the version that computed it, so a formula change bumps
    the version and leaves history alone rather than silently reinterpreting it.

    This is also §16's research dataset. Every row is a moment at which a signal
    *could* have fired, which makes it the denominator for §17's questions —
    "how many signals were there" is unanswerable without knowing how many
    chances there were.
    """

    __tablename__ = "feature_observations"
    __table_args__ = (
        # One vector per token per instant. Re-evaluating the same moment must
        # not create a second row: the dataset would silently double-weight it.
        UniqueConstraint("token_id", "observed_at", name="uq_feature_observations_token_time"),
        Index("ix_feature_observations_token_observed", "token_id", "observed_at"),
        Index("ix_feature_observations_observed_at", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
    )
    token_address: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # JSON rather than 41 columns: the set changes with the version, and a
    # migration per feature would make adding one expensive enough to discourage
    # it. Queried rarely; exported in bulk.
    features: Mapped[dict[str, Any]] = mapped_column(_JSONB, nullable=False)
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SignalRow(Base):
    """A research signal. Never an order.

    Points at the observation it was computed from rather than copying the
    vector: §11's immutability is a property of ``feature_observations``, and
    duplicating the values here would create a second copy that could disagree
    with the first.
    """

    __tablename__ = "signals"
    __table_args__ = (
        # A strategy fires at most once per observation. Without this, a replay
        # or a restart mid-pass would double-count the same signal.
        UniqueConstraint("observation_id", "strategy", name="uq_signals_observation_strategy"),
        Index("ix_signals_token_created", "token_id", "created_at"),
        Index("ix_signals_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    observation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("feature_observations.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
    )
    token_address: Mapped[str] = mapped_column(String(64), nullable=False)

    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # The observation's timestamp, not wall-clock now: spec §13 needs this
    # comparable with the data it was computed from, and stamping the current
    # time would fold our own processing delay into the signal's age.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Every clause and whether it held. §17 asks how results vary with age,
    # liquidity and activity, which needs to know which clause was binding.
    conditions: Mapped[list[Any]] = mapped_column(_JSONB, nullable=False)
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TradeState(enum.StrEnum):
    """Where a paper trade is in its life.

    PENDING exists because spec §14 asks for latency to be modelled: an order
    decided at T is not filled at T, it is filled against whatever the curve
    looks like once it arrives.
    """

    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ExitReason(enum.StrEnum):
    """Spec §14's exit reasons, exactly."""

    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TIMEOUT = "TIMEOUT"
    RISK_EXIT = "RISK_EXIT"
    MANUAL = "MANUAL"


class RiskDecisionRow(Base):
    """Every risk verdict, approved or not (spec §13).

    Rejections are persisted rather than logged: §17 asks how results vary with
    token age, liquidity and activity, and a rejected signal is a data point
    about the strategy's *reach* that a log line cannot be joined against.
    """

    __tablename__ = "risk_decisions"
    __table_args__ = (
        # One verdict per signal. A retry after a restart must not create a
        # second decision that could disagree with the first.
        UniqueConstraint("signal_id", name="uq_risk_decisions_signal"),
        Index("ix_risk_decisions_decided", "decision_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False
    )
    token_address: Mapped[str] = mapped_column(String(64), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # All three required by §13 on every signal.
    signal_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_age_ms: Mapped[float] = mapped_column(Float, nullable=False)
    position_size_sol: Mapped[float] = mapped_column(Float, nullable=False)
    checks: Mapped[list[Any]] = mapped_column(_JSONB, nullable=False)


class PaperTrade(Base):
    """A simulated trade. Spec §14's field list, and nothing that could execute.

    Fees and slippage are stored separately from PnL rather than folded in, so
    a result can be read as "the edge before friction" and "what friction took"
    — which is the whole question §17 exists to answer.
    """

    __tablename__ = "paper_trades"
    __table_args__ = (
        # A signal produces at most one trade, whatever restarts happen.
        UniqueConstraint("signal_id", name="uq_paper_trades_signal"),
        Index("ix_paper_trades_state", "state"),
        Index("ix_paper_trades_token_entry", "token_id", "entry_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False
    )
    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
    )
    token_address: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[TradeState] = mapped_column(
        Enum(TradeState, name="trade_state", native_enum=True, validate_strings=True),
        nullable=False,
        default=TradeState.PENDING,
    )
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # decision_at + modelled latency. The order fills against the first snapshot
    # at or after this, not against the one the decision was made on.
    submit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    entry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float | None] = mapped_column(Float)
    position_size_sol: Mapped[float] = mapped_column(Float, nullable=False)
    entry_tokens: Mapped[float | None] = mapped_column(Float)
    entry_fee_sol: Mapped[float | None] = mapped_column(Float)
    entry_slippage: Mapped[float | None] = mapped_column(Float)

    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_sol: Mapped[float | None] = mapped_column(Float)
    exit_fee_sol: Mapped[float | None] = mapped_column(Float)
    exit_slippage: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[ExitReason | None] = mapped_column(
        Enum(ExitReason, name="exit_reason", native_enum=True, validate_strings=True)
    )

    # Highest price seen while open, for the trailing stop. Persisted so a
    # restart cannot reset a trailing stop back to the entry price.
    peak_price: Mapped[float | None] = mapped_column(Float)

    gross_pnl_sol: Mapped[float | None] = mapped_column(Float)
    fees_sol: Mapped[float | None] = mapped_column(Float)
    slippage_cost_sol: Mapped[float | None] = mapped_column(Float)
    net_pnl_sol: Mapped[float | None] = mapped_column(Float)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
