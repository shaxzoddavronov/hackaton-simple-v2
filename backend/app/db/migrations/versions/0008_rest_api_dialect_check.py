"""extend dialect CHECK to include rest_api

Phase 12 adds REST API (OpenAPI / CRM / ERP / 1C OData) as the 10th
supported dialect. The ``workspace_connections.dialect`` and
``query_history.dialect`` CHECK constraints are pinned to a closed
allow-list, so they must be widened before a row carrying
``dialect='rest_api'`` can be inserted.

Mirrors migrations 0005 / 0007 in structure. The down-migration
restores the pre-rest_api allow-list, which is safe only if no row
references ``'rest_api'`` at downgrade time.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-27 13:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
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
                "'oracle','mongodb','elasticsearch','duckdb','mssql',"
                "'rest_api'"
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
                "'oracle','mongodb','elasticsearch','duckdb','mssql'"
                "))"
            )
