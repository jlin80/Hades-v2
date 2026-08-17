"""tokens: add backfill_attempts

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Bounded retry budget for the created_at backfill. Measured need: some
    # mints returned 404 from pump.fun on every attempt, minutes apart, and
    # without a budget they would be re-requested indefinitely.
    op.add_column(
        "tokens",
        sa.Column("backfill_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # Partial index: the backfill only ever scans rows missing a timestamp, and
    # that set stays small while `tokens` grows without bound.
    op.create_index(
        "ix_tokens_backfill_pending",
        "tokens",
        ["backfill_attempts", "discovered_at"],
        postgresql_where=sa.text("created_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_tokens_backfill_pending", table_name="tokens")
    op.drop_column("tokens", "backfill_attempts")
