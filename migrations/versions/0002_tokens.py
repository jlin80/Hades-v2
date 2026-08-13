"""tokens

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-13

The first real table: one row per discovered token.

The unique constraint on token_address is not decoration. It is what makes
discovery idempotent (task.md §13): the service inserts with ON CONFLICT DO
NOTHING, so processing the same token twice — across restarts, across
overlapping runs, or because two providers reported it — cannot create a second
row. Enforcing that in application code instead would be a race.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_address", sa.String(length=64), nullable=False),
        # NULL when the provider does not supply it. Never invented (task.md §7).
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=True),
        # When we first observed the token.
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        # When the pool was created, per the provider: the token's real age.
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pool_address", sa.String(length=64), nullable=True),
        sa.Column("discovery_provider", sa.String(length=64), nullable=False),
        # The provider's own object, verbatim, so an upstream schema change can
        # be detected after the fact rather than silently losing fields.
        sa.Column("raw_provider_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tokens")),
        sa.UniqueConstraint("token_address", name=op.f("uq_tokens_token_address")),
    )
    # Discovery and the Phase 4 report both scan by discovery time.
    op.create_index("ix_tokens_discovered_at", "tokens", ["discovered_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tokens_discovered_at", table_name="tokens")
    op.drop_table("tokens")
