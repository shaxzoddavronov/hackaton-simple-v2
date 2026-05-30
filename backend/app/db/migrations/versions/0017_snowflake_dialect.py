"""extend dialect CHECK to include 'snowflake'

Phase 28 — Snowflake is the 11th supported dialect. The
``workspace_connections.dialect`` and ``query_history.dialect``
CHECK constraints are pinned to a closed allow-list, so they need
widening before a row carrying ``dialect='snowflake'`` can be
inserted.

Mirrors 0005 / 0007 / 0008 in structure.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-30 16:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
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
                "'oracle','mongodb','elasticsearch','duckdb',"
                "'mssql','rest_api','snowflake'"
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
                "'oracle','mongodb','elasticsearch','duckdb',"
                "'mssql','rest_api'"
                "))"
            )
