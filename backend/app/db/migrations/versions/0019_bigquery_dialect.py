"""extend dialect CHECK to include 'bigquery'

Phase 30 — BigQuery is the 12th supported dialect. Mirrors
0005 / 0007 / 0017 in structure.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-30 18:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0019"
down_revision: Union[str, Sequence[str], None] = "0018"
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
                "'mssql','rest_api','snowflake','bigquery'"
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
                "'mssql','rest_api','snowflake'"
                "))"
            )
