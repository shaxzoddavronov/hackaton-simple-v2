"""add connection_clusters table + workspace_connections.cluster_id (Phase 42)

A **ConnectionCluster** is a logical grouping of N workspace
connections that act as ONE distributed database from the user's
perspective — e.g. "Production Postgres" with read replicas, or
"Quiz cluster" with three sharded ClickHouse hosts. Each
WorkspaceConnection joins at most one cluster (nullable FK).

This unlocks the Phase 42 scope picker: a chat turn can scope to
{ table, database, cluster, all clusters, all connections } and
the agent fans out the federation path accordingly.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-04 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: Union[str, Sequence[str], None] = "0026"
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
        "connection_clusters",
        sa.Column(
            "id",
            uuid_col,
            primary_key=True,
            server_default=uuid_default,
        ),
        sa.Column("workspace_id", uuid_col, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "now()" if _is_postgres() else "CURRENT_TIMESTAMP"
            ),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "now()" if _is_postgres() else "CURRENT_TIMESTAMP"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_connection_clusters_workspace_name"
        ),
    )
    op.create_index(
        "ix_connection_clusters_workspace_id",
        "connection_clusters",
        ["workspace_id"],
    )

    # Add cluster_id to workspace_connections (nullable; a connection
    # may live outside any cluster — useful for one-off DBs).
    op.add_column(
        "workspace_connections",
        sa.Column("cluster_id", uuid_col, nullable=True),
    )
    op.create_foreign_key(
        "fk_workspace_connections_cluster_id",
        "workspace_connections",
        "connection_clusters",
        ["cluster_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_workspace_connections_cluster_id",
        "workspace_connections",
        ["cluster_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_connections_cluster_id",
        table_name="workspace_connections",
    )
    op.drop_constraint(
        "fk_workspace_connections_cluster_id",
        "workspace_connections",
        type_="foreignkey",
    )
    op.drop_column("workspace_connections", "cluster_id")

    op.drop_index(
        "ix_connection_clusters_workspace_id",
        table_name="connection_clusters",
    )
    op.drop_table("connection_clusters")
