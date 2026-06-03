"""cache result rows on query_history for the export endpoints (Phase 34)

The export endpoints (CSV / Excel / JSON download) need to return the
exact rows the user saw in the chat answer without re-running the
query — re-running breaks for federated turns (where ``sql_text`` is
a multi-section concatenation) and would charge the user the query
cost twice. Cheaper to store the rows once at executor time.

Two new nullable columns on ``query_history``:
  * ``result_columns`` — JSON list[str] of column names.
  * ``result_rows``    — JSON list[list[Any]] of the row values.

Both stay NULL for oversize results (the executor enforces a
configurable row + byte cap so a million-row query doesn't blow the
metadata DB). The export endpoint surfaces NULL as HTTP 413.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-03 09:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, Sequence[str], None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    # Use JSONB on Postgres (indexable, smaller, faster) and plain
    # JSON on SQLite (which doesn't ship JSONB). Mirrors how the
    # existing JSONType variant on models.py picks per dialect.
    json_col = postgresql.JSONB() if _is_postgres() else sa.JSON()
    op.add_column(
        "query_history",
        sa.Column("result_columns", json_col, nullable=True),
    )
    op.add_column(
        "query_history",
        sa.Column("result_rows", json_col, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("query_history", "result_rows")
    op.drop_column("query_history", "result_columns")
