"""add dashboards + saved_questions tables

Phase 26 — users star chat messages they want to re-run later. The
saved questions get filed under a Dashboard which the user opens
to see every starred question's current answer in card form.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-30 14:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    uuid_col = (
        postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)
    )
    uuid_default = sa.text(
        "gen_random_uuid()"
        if _is_postgres()
        else "(lower(hex(randomblob(16))))"
    )

    op.create_table(
        "dashboards",
        sa.Column(
            "id", uuid_col, primary_key=True, server_default=uuid_default
        ),
        sa.Column(
            "owner_id",
            uuid_col,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            uuid_col,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_dashboards_workspace_name"
        ),
    )
    op.create_index(
        "ix_dashboards_owner_id", "dashboards", ["owner_id"]
    )
    op.create_index(
        "ix_dashboards_workspace_id", "dashboards", ["workspace_id"]
    )

    op.create_table(
        "saved_questions",
        sa.Column(
            "id", uuid_col, primary_key=True, server_default=uuid_default
        ),
        sa.Column(
            "owner_id",
            uuid_col,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            uuid_col,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dashboard_id",
            uuid_col,
            sa.ForeignKey("dashboards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "connection_id",
            uuid_col,
            sa.ForeignKey(
                "workspace_connections.id", ondelete="SET NULL"
            ),
            nullable=True,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_saved_questions_owner_id", "saved_questions", ["owner_id"]
    )
    op.create_index(
        "ix_saved_questions_workspace_id",
        "saved_questions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_saved_questions_dashboard_id",
        "saved_questions",
        ["dashboard_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_saved_questions_dashboard_id", "saved_questions")
    op.drop_index("ix_saved_questions_workspace_id", "saved_questions")
    op.drop_index("ix_saved_questions_owner_id", "saved_questions")
    op.drop_table("saved_questions")
    op.drop_index("ix_dashboards_workspace_id", "dashboards")
    op.drop_index("ix_dashboards_owner_id", "dashboards")
    op.drop_table("dashboards")
