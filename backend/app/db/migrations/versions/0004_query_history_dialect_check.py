"""extend query_history.dialect CHECK to match WorkspaceConnection dialects

Migrations 0001/0003 introduced new connection dialects (mysql,
clickhouse, oracle, mongodb, elasticsearch) but left the
``query_history.dialect`` CHECK constraint pinned to the original
``('postgres','sqlite')`` allow-list. The audit-log INSERT now fails
the CHECK whenever the agent executes against any of the new dialects,
which manifests as ``ERR_INCOMPLETE_CHUNKED_ENCODING`` on the chat SSE
stream (the chat persist block is outside the in-flight try/except).

This migration widens the CHECK to mirror
``workspace_connections.dialect``. Down-migration restores the
original two-dialect constraint — that's safe only if no
``query_history`` row references one of the wider values.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26 22:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute(
            "ALTER TABLE query_history "
            "DROP CONSTRAINT IF EXISTS ck_query_history_dialect"
        )
        op.execute(
            "ALTER TABLE query_history ADD CONSTRAINT ck_query_history_dialect "
            "CHECK (dialect IN ("
            "'postgres','sqlite','mysql','clickhouse',"
            "'oracle','mongodb','elasticsearch'"
            "))"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "ALTER TABLE query_history "
            "DROP CONSTRAINT IF EXISTS ck_query_history_dialect"
        )
        op.execute(
            "ALTER TABLE query_history ADD CONSTRAINT ck_query_history_dialect "
            "CHECK (dialect IN ('postgres','sqlite'))"
        )
