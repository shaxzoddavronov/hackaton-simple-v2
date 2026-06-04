"""extend rag_chunks.kind CHECK to include 'qa_history' (Phase 38)

After every successful chat turn we embed the (user question +
assistant headline) pair as a new rag_chunks row of kind
``qa_history``. The next turn's coordinator semantic-searches this
sub-index; high-similarity hits surface as "you asked this before"
chips in the UI.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-04 00:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0026"
down_revision: Union[str, Sequence[str], None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute(
        "ALTER TABLE rag_chunks DROP CONSTRAINT IF EXISTS ck_rag_chunks_kind"
    )
    op.execute(
        "ALTER TABLE rag_chunks ADD CONSTRAINT ck_rag_chunks_kind "
        "CHECK (kind IN ("
        "'schema_table','schema_column','api_endpoint',"
        "'user_doc','harvested_doc','qa_history'"
        "))"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(
        "ALTER TABLE rag_chunks DROP CONSTRAINT IF EXISTS ck_rag_chunks_kind"
    )
    op.execute(
        "ALTER TABLE rag_chunks ADD CONSTRAINT ck_rag_chunks_kind "
        "CHECK (kind IN ("
        "'schema_table','schema_column','api_endpoint',"
        "'user_doc','harvested_doc'"
        "))"
    )
