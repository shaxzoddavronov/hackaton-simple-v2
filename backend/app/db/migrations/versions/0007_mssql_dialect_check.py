"""extend dialect CHECK to include mssql

Phase 11 adds Microsoft SQL Server as a supported dialect. The
``workspace_connections.dialect`` and ``query_history.dialect`` CHECK
constraints are pinned to a closed allow-list, so they must be widened
before a row carrying ``dialect='mssql'`` can be inserted.

This mirrors migration 0005 in structure. The down-migration restores
the pre-MSSQL allow-list, which is safe only if no row references
``'mssql'`` at downgrade time.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-27 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        for table, constraint in (
            ("workspace_connections", "ck_workspace_connections_dialect"),
            ("query_history", "ck_query_history_dialect"),
        ):
            op.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}"
            )
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                "CHECK (dialect IN ("
                "'postgres','sqlite','mysql','clickhouse',"
                "'oracle','mongodb','elasticsearch','duckdb','mssql'"
                "))"
            )


def downgrade() -> None:
    if _is_postgres():
        for table, constraint in (
            ("workspace_connections", "ck_workspace_connections_dialect"),
            ("query_history", "ck_query_history_dialect"),
        ):
            op.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}"
            )
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                "CHECK (dialect IN ("
                "'postgres','sqlite','mysql','clickhouse',"
                "'oracle','mongodb','elasticsearch','duckdb'"
                "))"
            )
