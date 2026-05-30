"""add report_schedules table for scheduled dashboard email delivery

Phase 29 — admin marks a dashboard for daily / weekly / cron-driven
email delivery. Celery beat scans this table every minute, fires due
schedules, and dispatches an HTML digest via SMTP.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-30 17:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
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
        "report_schedules",
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
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cron", sa.String(64), nullable=False),
        sa.Column(
            "recipients",
            sa.String(2048),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "last_fired_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_status", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_report_schedules_workspace_id",
        "report_schedules",
        ["workspace_id"],
    )
    op.create_index(
        "ix_report_schedules_dashboard_id",
        "report_schedules",
        ["dashboard_id"],
    )
    op.create_index(
        "ix_report_schedules_enabled", "report_schedules", ["enabled"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_report_schedules_enabled", table_name="report_schedules"
    )
    op.drop_index(
        "ix_report_schedules_dashboard_id", table_name="report_schedules"
    )
    op.drop_index(
        "ix_report_schedules_workspace_id", table_name="report_schedules"
    )
    op.drop_table("report_schedules")
