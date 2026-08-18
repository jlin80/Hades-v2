"""observation_outcomes

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observation_outcomes",
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
        # One row per (observation, scheme): §15 asks for several barrier
        # configurations, since "+30% before -20%?" is only one question.
        sa.Column("label_config", sa.String(64), nullable=False),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("barrier_hit_at_seconds", sa.Float(), nullable=True),
        sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("return_1m", sa.Float(), nullable=True),
        sa.Column("return_5m", sa.Float(), nullable=True),
        sa.Column("return_15m", sa.Float(), nullable=True),
        sa.Column("return_30m", sa.Float(), nullable=True),
        sa.Column("return_1h", sa.Float(), nullable=True),
        sa.Column("mfe", sa.Float(), nullable=True),
        sa.Column("mae", sa.Float(), nullable=True),
        sa.Column("mfe_at_seconds", sa.Float(), nullable=True),
        sa.Column("mae_at_seconds", sa.Float(), nullable=True),
        sa.Column("observations_after", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_observation_outcomes_config",
        "observation_outcomes",
        ["observation_id", "label_config"],
    )
    # The hot query: which labels are still provisional and need recomputing.
    op.create_index(
        "ix_observation_outcomes_pending",
        "observation_outcomes",
        ["is_final", "computed_at"],
    )
    op.create_index(
        "ix_observation_outcomes_label", "observation_outcomes", ["label_config", "label"]
    )


def downgrade() -> None:
    op.drop_index("ix_observation_outcomes_label", table_name="observation_outcomes")
    op.drop_index("ix_observation_outcomes_pending", table_name="observation_outcomes")
    op.drop_table("observation_outcomes")
