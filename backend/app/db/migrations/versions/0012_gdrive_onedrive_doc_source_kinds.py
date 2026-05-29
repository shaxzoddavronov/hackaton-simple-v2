"""widen doc_sources.source_kind CHECK to include 'gdrive' + 'onedrive'

Phase 17.4 adds Google Drive and OneDrive (Microsoft Graph) as new
DocSource kinds. The harvester / API code is dialect-agnostic; only
the CHECK constraint needs updating so the DB doesn't reject new rows.

We drop+re-add the constraint on Postgres (mirroring the pattern from
0007/0008/0009) and no-op on SQLite where CHECK is enforced at table
creation only and the unit-test schema already accepts arbitrary
strings via the test fixtures.

Note for parallel SMB migration (0011): if both this and 0011 land in
the same migration run, alembic applies them sequentially and the
final state lists ``{folder, url_list, db_column, smb, gdrive,
onedrive}`` — we include ``smb`` in our re-add too so the order of
application doesn't matter.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-29 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute(
            "ALTER TABLE doc_sources "
            "DROP CONSTRAINT IF EXISTS ck_doc_sources_kind"
        )
        op.execute(
            "ALTER TABLE doc_sources ADD CONSTRAINT ck_doc_sources_kind "
            "CHECK (source_kind IN ("
            "'folder','url_list','db_column','smb','gdrive','onedrive'"
            "))"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "ALTER TABLE doc_sources "
            "DROP CONSTRAINT IF EXISTS ck_doc_sources_kind"
        )
        # Revert to the SMB-only set (matching 0011's post-state).
        op.execute(
            "ALTER TABLE doc_sources ADD CONSTRAINT ck_doc_sources_kind "
            "CHECK (source_kind IN ("
            "'folder','url_list','db_column','smb'"
            "))"
        )
