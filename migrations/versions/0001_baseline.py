"""baseline

Revision ID: 0001
Revises:
Create Date: 2026-08-13

Phase 0 defines no tables, and this migration deliberately creates none.

Its purpose is to establish the migration chain and the ``alembic_version``
table, which is a real, verifiable outcome: the compose stack's one-shot
``migrate`` service must apply it successfully before the API is allowed to
start, and ``GET /status`` reads the resulting revision back out of the
database. That makes the whole toolchain — settings -> async engine -> alembic
-> API readback — exercised end to end from the very first commit.

Inventing a placeholder table here to make the schema look non-empty would be
building ahead of the phase (task.md §1). The first real table arrives in
Phase 1 with token discovery.
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema changes: this revision only establishes the chain."""


def downgrade() -> None:
    """No schema changes to reverse."""
