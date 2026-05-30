"""widen doc_sources.source_kind CHECK to include 'slack'

Phase 21 adds Slack workspace exports (the standard "Export
workspace data" ZIP) as a DocSource kind. Each thread becomes one
RAG document with a synthetic row_context tying it to its source
channel + parent ``ts``, mirroring Phase 17.1 db_column / Phase 19
IMAP linkage patterns.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-30 09:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute(
            "ALTER TABLE doc_sources DROP CONSTRAINT IF EXISTS ck_doc_sources_kind"
        )
        op.execute(
            "ALTER TABLE doc_sources ADD CONSTRAINT ck_doc_sources_kind "
            "CHECK (source_kind IN ("
            "'folder','url_list','db_column','smb','gdrive','onedrive',"
            "'imap','slack'"
            "))"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "ALTER TABLE doc_sources DROP CONSTRAINT IF EXISTS ck_doc_sources_kind"
        )
        op.execute(
            "ALTER TABLE doc_sources ADD CONSTRAINT ck_doc_sources_kind "
            "CHECK (source_kind IN ("
            "'folder','url_list','db_column','smb','gdrive','onedrive','imap'"
            "))"
        )
