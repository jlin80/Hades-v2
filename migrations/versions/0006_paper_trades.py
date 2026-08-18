"""risk_decisions and paper_trades

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRADE_STATES = ("PENDING", "OPEN", "CLOSED", "CANCELLED")
EXIT_REASONS = (
    "TAKE_PROFIT",
    "STOP_LOSS",
    "TRAILING_STOP",
    "TIMEOUT",
    "RISK_EXIT",
    "MANUAL",
)


def upgrade() -> None:
    # Every verdict, approved or not. Rejections are data about the strategy's
    # reach, and a log line cannot be joined against.
    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "signal_id",
            sa.Uuid(),
            sa.ForeignKey("signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_address", sa.String(64), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("signal_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_age_ms", sa.Float(), nullable=False),
        sa.Column("position_size_sol", sa.Float(), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_unique_constraint("uq_risk_decisions_signal", "risk_decisions", ["signal_id"])
    op.create_index("ix_risk_decisions_decided", "risk_decisions", ["decision_at"])

    trade_state = postgresql.ENUM(*TRADE_STATES, name="trade_state", create_type=False)
    trade_state.create(op.get_bind(), checkfirst=True)
    exit_reason = postgresql.ENUM(*EXIT_REASONS, name="exit_reason", create_type=False)
    exit_reason.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "signal_id",
            sa.Uuid(),
            sa.ForeignKey("signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_id", sa.Uuid(), sa.ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_address", sa.String(64), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("state", trade_state, nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("position_size_sol", sa.Float(), nullable=False),
        sa.Column("entry_tokens", sa.Float(), nullable=True),
        sa.Column("entry_fee_sol", sa.Float(), nullable=True),
        sa.Column("entry_slippage", sa.Float(), nullable=True),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_sol", sa.Float(), nullable=True),
        sa.Column("exit_fee_sol", sa.Float(), nullable=True),
        sa.Column("exit_slippage", sa.Float(), nullable=True),
        sa.Column("exit_reason", exit_reason, nullable=True),
        sa.Column("peak_price", sa.Float(), nullable=True),
        # Kept apart from PnL rather than folded in, so a result reads as "the
        # edge before friction" and "what friction took".
        sa.Column("gross_pnl_sol", sa.Float(), nullable=True),
        sa.Column("fees_sol", sa.Float(), nullable=True),
        sa.Column("slippage_cost_sol", sa.Float(), nullable=True),
        sa.Column("net_pnl_sol", sa.Float(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # A signal produces at most one trade, whatever restarts happen.
    op.create_unique_constraint("uq_paper_trades_signal", "paper_trades", ["signal_id"])
    op.create_index("ix_paper_trades_state", "paper_trades", ["state"])
    op.create_index("ix_paper_trades_token_entry", "paper_trades", ["token_id", "entry_time"])


def downgrade() -> None:
    op.drop_index("ix_paper_trades_token_entry", table_name="paper_trades")
    op.drop_index("ix_paper_trades_state", table_name="paper_trades")
    op.drop_table("paper_trades")
    postgresql.ENUM(name="exit_reason").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="trade_state").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_risk_decisions_decided", table_name="risk_decisions")
    op.drop_table("risk_decisions")
