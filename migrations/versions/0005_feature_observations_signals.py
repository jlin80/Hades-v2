"""feature_observations and signals

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Spec §11's immutable T0 snapshot, and §16's research dataset. No mutable
    # column at all: a decision's features must stay exactly as they were.
    op.create_table(
        "feature_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "token_id", sa.Uuid(), sa.ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_address", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_version", sa.String(32), nullable=False),
        # JSONB on Postgres: indexable and queryable, unlike a text blob, which
        # matters because §17's questions slice the dataset by feature value.
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "stored_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Re-evaluating the same instant must not double-weight it in the dataset.
    op.create_unique_constraint(
        "uq_feature_observations_token_time", "feature_observations", ["token_id", "observed_at"]
    )
    op.create_index(
        "ix_feature_observations_token_observed",
        "feature_observations",
        ["token_id", "observed_at"],
    )
    op.create_index("ix_feature_observations_observed_at", "feature_observations", ["observed_at"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "observation_id",
            sa.Uuid(),
            sa.ForeignKey("feature_observations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_id", sa.Uuid(), sa.ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_address", sa.String(64), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "stored_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # A strategy fires at most once per observation: a replay or a restart
    # mid-pass must not double-count the same signal.
    op.create_unique_constraint(
        "uq_signals_observation_strategy", "signals", ["observation_id", "strategy"]
    )
    op.create_index("ix_signals_token_created", "signals", ["token_id", "created_at"])
    op.create_index("ix_signals_created_at", "signals", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_signals_created_at", table_name="signals")
    op.drop_index("ix_signals_token_created", table_name="signals")
    op.drop_table("signals")

    op.drop_index("ix_feature_observations_observed_at", table_name="feature_observations")
    op.drop_index("ix_feature_observations_token_observed", table_name="feature_observations")
    op.drop_table("feature_observations")
