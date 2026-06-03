"""add connection-health columns on workspace_connections (Phase 35)

Periodic Celery-beat task pokes every workspace_connection with a
dialect-appropriate liveness probe (SELECT 1 / GET /v1/info / ping)
and writes the outcome onto the row so the UI can render a green /
red status dot without re-doing the probe on every page load.

Four new nullable columns — health probes start clean per row, and
existing rows simply read as "never checked" (NULL last_health_ok).

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-04 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, Sequence[str], None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspace_connections",
        sa.Column(
            "last_health_check_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "workspace_connections",
        sa.Column("last_health_ok", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "workspace_connections",
        sa.Column("last_health_latency_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workspace_connections",
        sa.Column("last_health_error", sa.Text(), nullable=True),
    )
    # Partial index so the "show me unhealthy connections" admin
    # query is cheap even with thousands of workspaces.
    op.create_index(
        "ix_workspace_connections_unhealthy",
        "workspace_connections",
        ["last_health_check_at"],
        postgresql_where=sa.text("last_health_ok IS FALSE"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_connections_unhealthy",
        table_name="workspace_connections",
    )
    op.drop_column("workspace_connections", "last_health_error")
    op.drop_column("workspace_connections", "last_health_latency_ms")
    op.drop_column("workspace_connections", "last_health_ok")
    op.drop_column("workspace_connections", "last_health_check_at")
