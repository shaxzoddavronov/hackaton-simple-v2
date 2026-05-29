"""widen doc_sources.source_kind CHECK to include 'smb'

Phase 17.2 adds SMB / CIFS network shares as a DocSource kind so
QueryMind can crawl Windows file servers, NAS appliances, and
NFS-via-Samba mounts. The harvester + API code is dialect-agnostic;
only the CHECK constraint needs updating so the DB doesn't reject
new rows.

We drop+re-add the constraint on Postgres (mirroring 0007/0008/0009).
SQLite enforces CHECK only at CREATE TABLE time so the unit-test
schema already accepts arbitrary strings — no-op there.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-29 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
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
            "'folder','url_list','db_column','smb'"
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
            "'folder','url_list','db_column'"
            "))"
        )
