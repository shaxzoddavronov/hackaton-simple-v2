"""rag layer: uploaded_documents + rag_chunks (pgvector)

Adds the persistent RAG index. The ``embedding`` column is created as
``vector(1024)`` on Postgres (via the pgvector extension) and as JSON on
SQLite — unit tests run against SQLite and compute cosine in Python.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-26 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PG = "postgresql"


def _is_postgres() -> bool:
    return op.get_context().dialect.name == _PG


def upgrade() -> None:
    if _is_postgres():
        # pgvector ships with most managed Postgres offerings now; the
        # extension is idempotent so re-running the migration is safe.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "uploaded_documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_uploaded_documents_owner_id", "uploaded_documents", ["owner_id"]
    )
    op.create_index(
        "ix_uploaded_documents_workspace_id",
        "uploaded_documents",
        ["workspace_id"],
    )

    # `embedding` column type depends on dialect — see module docstring.
    if _is_postgres():
        embedding_col = sa.Column("embedding", sa.dialects.postgresql.ARRAY(sa.Float()), nullable=True)
        # We declare a real vector(1024) column directly via raw SQL after
        # the table is created so SQLAlchemy's ARRAY placeholder above is
        # never actually used. (Alembic does not ship a pgvector type.)
    else:
        embedding_col = sa.Column("embedding", sa.JSON(), nullable=True)

    op.create_table(
        "rag_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("uploaded_documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        embedding_col,
        sa.Column(
            "chunk_metadata",
            postgresql.JSONB() if _is_postgres() else sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if _is_postgres() else sa.text("'{}'"),
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('schema_table','schema_column','api_endpoint','user_doc')",
            name="ck_rag_chunks_kind",
        ),
        sa.UniqueConstraint(
            "workspace_id", "kind", "source_key",
            name="uq_rag_chunks_workspace_kind_source",
        ),
    )

    # Swap the placeholder ARRAY for the real pgvector column on Postgres.
    if _is_postgres():
        op.execute("ALTER TABLE rag_chunks DROP COLUMN embedding")
        op.execute("ALTER TABLE rag_chunks ADD COLUMN embedding vector(1024)")
        # HNSW index for cosine distance — best recall/latency tradeoff for
        # the workspace-scoped retrievals we run.
        op.execute(
            "CREATE INDEX ix_rag_chunks_embedding_hnsw "
            "ON rag_chunks USING hnsw (embedding vector_cosine_ops)"
        )

    op.create_index("ix_rag_chunks_workspace_id", "rag_chunks", ["workspace_id"])
    op.create_index("ix_rag_chunks_kind", "rag_chunks", ["kind"])
    op.create_index("ix_rag_chunks_document_id", "rag_chunks", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_rag_chunks_document_id", table_name="rag_chunks")
    op.drop_index("ix_rag_chunks_kind", table_name="rag_chunks")
    op.drop_index("ix_rag_chunks_workspace_id", table_name="rag_chunks")
    if _is_postgres():
        op.execute("DROP INDEX IF EXISTS ix_rag_chunks_embedding_hnsw")
    op.drop_table("rag_chunks")
    op.drop_index(
        "ix_uploaded_documents_workspace_id", table_name="uploaded_documents"
    )
    op.drop_index(
        "ix_uploaded_documents_owner_id", table_name="uploaded_documents"
    )
    op.drop_table("uploaded_documents")
    # Leave the `vector` extension installed; other apps may depend on it.
