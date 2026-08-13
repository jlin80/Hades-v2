"""initial: tokens table

Revision ID: 0001
Revises:
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOKEN_STATES = (
    "CREATED",
    "DISCOVERED",
    "TRACKING",
    "ACTIVE",
    "MIGRATED",
    "INACTIVE",
    "DEAD",
)


def upgrade() -> None:
    token_state = postgresql.ENUM(*TOKEN_STATES, name="token_state", create_type=False)
    token_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_address", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("creator_address", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("state", token_state, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Unique on the mint is what makes discovery idempotent across restarts.
    op.create_unique_constraint("uq_tokens_token_address", "tokens", ["token_address"])
    op.create_index("ix_tokens_state_discovered_at", "tokens", ["state", "discovered_at"])
    op.create_index("ix_tokens_created_at", "tokens", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_tokens_created_at", table_name="tokens")
    op.drop_index("ix_tokens_state_discovered_at", table_name="tokens")
    op.drop_constraint("uq_tokens_token_address", "tokens", type_="unique")
    op.drop_table("tokens")
    postgresql.ENUM(name="token_state").drop(op.get_bind(), checkfirst=True)
