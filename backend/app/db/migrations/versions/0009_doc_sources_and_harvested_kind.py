"""add doc_sources table + 'harvested_doc' RagChunk kind

Phase 14 introduces cross-source document harvesting:
  * Three crawl sources — local folder, explicit URL list, DB column
    that holds file paths / URLs.
  * Each is stored as a ``doc_sources`` row owned by a workspace.
  * Chunks produced by the harvester land in the existing
    ``rag_chunks`` table with the new ``'harvested_doc'`` ``kind``,
    so the retriever picks them up alongside schema chunks and
    uploaded docs without further changes.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-27 14:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    # 1) Widen RagChunk.kind CHECK so harvested chunks can be inserted.
    if _is_postgres():
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

    # 2) doc_sources table.
    json_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if _is_postgres()
        else sa.JSON()
    )
    op.create_table(
        "doc_sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36),
            primary_key=True,
            server_default=sa.text(
                "gen_random_uuid()" if _is_postgres() else "(lower(hex(randomblob(16))))"
            ),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column(
            "config",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb" if _is_postgres() else "'{}'"),
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'idle'"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "last_harvested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "doc_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source_kind IN ('folder','url_list','db_column')",
            name="ck_doc_sources_kind",
        ),
        sa.CheckConstraint(
            "status IN ('idle','harvesting','ready','error')",
            name="ck_doc_sources_status",
        ),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_doc_sources_workspace_name"
        ),
    )
    op.create_index(
        "ix_doc_sources_workspace_id",
        "doc_sources",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_doc_sources_workspace_id", table_name="doc_sources")
    op.drop_table("doc_sources")
    if _is_postgres():
        op.execute(
            "ALTER TABLE rag_chunks DROP CONSTRAINT IF EXISTS ck_rag_chunks_kind"
        )
        op.execute(
            "ALTER TABLE rag_chunks ADD CONSTRAINT ck_rag_chunks_kind "
            "CHECK (kind IN ("
            "'schema_table','schema_column','api_endpoint','user_doc'"
            "))"
        )
