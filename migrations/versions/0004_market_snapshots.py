"""market_snapshots, and the tracking columns on tokens

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tokens", sa.Column("tracking_started_at", sa.DateTime(timezone=True)))
    op.add_column("tokens", sa.Column("next_snapshot_at", sa.DateTime(timezone=True)))
    op.add_column("tokens", sa.Column("last_snapshot_at", sa.DateTime(timezone=True)))
    op.add_column(
        "tokens",
        sa.Column("snapshot_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "tokens",
        sa.Column("snapshot_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # The hot query: "which tracked tokens are due now?". Partial, because only
    # tokens actually in tracking have a due time, and that set is bounded by
    # the concurrency limit while `tokens` grows without bound.
    op.create_index(
        "ix_tokens_next_snapshot",
        "tokens",
        ["next_snapshot_at"],
        postgresql_where=sa.text("next_snapshot_at IS NOT NULL"),
    )

    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "token_id",
            sa.Uuid(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_address", sa.String(64), nullable=False),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("token_age_seconds", sa.Float(), nullable=True),
        sa.Column("tier", sa.String(16), nullable=False),
        # BigInteger: total_supply is 1e15 and overflows a 32-bit column.
        sa.Column("virtual_sol_reserves", sa.BigInteger(), nullable=True),
        sa.Column("virtual_token_reserves", sa.BigInteger(), nullable=True),
        sa.Column("real_sol_reserves", sa.BigInteger(), nullable=True),
        sa.Column("real_token_reserves", sa.BigInteger(), nullable=True),
        sa.Column("total_supply", sa.BigInteger(), nullable=True),
        sa.Column("base_decimals", sa.Integer(), nullable=True),
        sa.Column("quote_decimals", sa.Integer(), nullable=True),
        sa.Column("price_sol", sa.Float(), nullable=True),
        sa.Column("market_cap_sol", sa.Float(), nullable=True),
        sa.Column("liquidity_sol", sa.Float(), nullable=True),
        sa.Column("market_cap_usd", sa.Float(), nullable=True),
        sa.Column("sol_price_usd", sa.Float(), nullable=True),
        sa.Column("is_complete", sa.Boolean(), nullable=True),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply_count", sa.Integer(), nullable=True),
        sa.Column("provider_data_age_seconds", sa.Float(), nullable=True),
        sa.Column("is_stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # The query every feature computation makes: one token's series, in order.
    op.create_index(
        "ix_market_snapshots_token_observed", "market_snapshots", ["token_id", "observed_at"]
    )
    op.create_index("ix_market_snapshots_observed_at", "market_snapshots", ["observed_at"])


def downgrade() -> None:
    op.drop_index("ix_market_snapshots_observed_at", table_name="market_snapshots")
    op.drop_index("ix_market_snapshots_token_observed", table_name="market_snapshots")
    op.drop_table("market_snapshots")

    op.drop_index("ix_tokens_next_snapshot", table_name="tokens")
    for column in (
        "snapshot_failures",
        "snapshot_count",
        "last_snapshot_at",
        "next_snapshot_at",
        "tracking_started_at",
    ):
        op.drop_column("tokens", column)
