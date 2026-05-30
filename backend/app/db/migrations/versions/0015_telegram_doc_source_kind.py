"""widen doc_sources.source_kind CHECK to include 'telegram'

Phase 22 adds Telegram Desktop's chat-export JSON as a DocSource
kind. Each yielded chunk is one chat-day's worth of messages with
synthetic row_context tying it to ``chat_id`` + ``date`` —
mirroring the per-thread linkage Phase 21 introduced for Slack.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-30 11:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
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
            "'imap','slack','telegram'"
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
            "'folder','url_list','db_column','smb','gdrive','onedrive',"
            "'imap','slack'"
            "))"
        )
