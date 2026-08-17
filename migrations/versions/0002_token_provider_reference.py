"""tokens: add raw_provider_reference

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The creation transaction signature, when the discovering source gives us
    # one. PumpPortal's creation event does; polling pump.fun does not.
    op.add_column("tokens", sa.Column("raw_provider_reference", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("tokens", "raw_provider_reference")
