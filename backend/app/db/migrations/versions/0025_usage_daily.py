"""add usage_daily table (Phase 37)

Per-workspace per-day counters: LLM calls + tokens, queries ok/failed,
RAG retrievals, cache hits. One row per (workspace_id, day) — chat
turns UPSERT into it from a request-scoped ContextVar bucket flushed
at the end of each agent run.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-04 00:20:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: Union[str, Sequence[str], None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    uuid_col = (
        postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)
    )
    op.create_table(
        "usage_daily",
        sa.Column("workspace_id", uuid_col, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "llm_calls",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "llm_tokens_in",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "llm_tokens_out",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "queries_ok",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "queries_failed",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rag_retrievals",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cache_hits",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()" if _is_postgres() else "CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id", "day"),
    )
    # The dashboard queries by date range, ordered by day desc.
    op.create_index(
        "ix_usage_daily_workspace_day",
        "usage_daily",
        ["workspace_id", "day"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_daily_workspace_day", table_name="usage_daily")
    op.drop_table("usage_daily")
