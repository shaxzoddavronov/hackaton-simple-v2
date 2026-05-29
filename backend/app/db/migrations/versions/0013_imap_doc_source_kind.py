"""widen doc_sources.source_kind CHECK to include 'imap'

Phase 19 adds IMAP email mailboxes as a DocSource kind. The
harvester (services/doc_harvest.harvest_imap) ingests message bodies
and attachments through the same RAG pipeline as folders / cloud
drives, but each chunk also carries a synthetic row_context tying it
to its source message (table="email", row_pk={message_id:...}) so
citations link the chunk back to the originating thread — same
linkage pattern Phase 17.1 introduced for db_column sources.

Mirrors 0011 / 0012 in structure: Postgres-guarded DROP + re-ADD of
the CHECK constraint. SQLite enforces CHECK at table-creation only
and the unit-test fixture lives in working-tree models.py, which is
already updated.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-29 14:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
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
            "'folder','url_list','db_column','smb','gdrive','onedrive','imap'"
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
            "'folder','url_list','db_column','smb','gdrive','onedrive'"
            "))"
        )
