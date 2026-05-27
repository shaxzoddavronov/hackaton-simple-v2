"""drop deprecated Workspace.dialect + Workspace.connection_meta columns

Phase 1's migration 0003 split connection-level details out of the
Workspace row into a per-row WorkspaceConnection. The legacy
``workspaces.dialect`` and ``workspaces.connection_meta`` columns
were kept nullable for one release so a rollback could rebuild from
them. Nine phases of releases later nothing reads them — confirmed
by ``grep -RE 'workspace\\.(dialect|connection_meta)|Workspace\\.(dialect|connection_meta)' backend/app`` returning no hits.

Dropping both columns plus the no-longer-relevant
``ck_workspaces_dialect`` CHECK constraint that gated their values.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-27 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        # CHECK first — depends on the column.
        op.execute(
            "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS ck_workspaces_dialect"
        )
        op.execute("ALTER TABLE workspaces DROP COLUMN IF EXISTS dialect")
        op.execute("ALTER TABLE workspaces DROP COLUMN IF EXISTS connection_meta")


def downgrade() -> None:
    """Best-effort restore. The original rows can't be reconstructed
    (the data moved to ``workspace_connections``), so on downgrade we
    just re-add the columns as nullable with no defaults — anyone
    rolling back has to repopulate them themselves."""
    if _is_postgres():
        op.execute(
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS dialect VARCHAR(32) NULL"
        )
        op.execute(
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS connection_meta JSONB NULL"
        )
        op.execute(
            "ALTER TABLE workspaces ADD CONSTRAINT ck_workspaces_dialect "
            "CHECK (dialect IS NULL OR dialect IN ("
            "'postgres','sqlite','mysql','clickhouse','duckdb',"
            "'oracle','mongodb','elasticsearch'"
            "))"
        )
