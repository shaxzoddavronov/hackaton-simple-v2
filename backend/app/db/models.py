"""SQLAlchemy 2.0 ORM models for the QueryMind application database.

The schema mirrors PLAN.md §"App Data Model": users own workspaces (each
of which is a connected end-user database), workspaces have credentials
and a profiled schema bundle, chat sessions hang off workspaces, and
every assistant turn writes a message + an audit row in `query_history`.

All primary keys are UUIDs generated server-side by Postgres
(`gen_random_uuid()` from the `pgcrypto` extension — enabled by the
initial Alembic migration). The same UUID columns work on SQLite during
unit tests because SQLAlchemy stores them as 36-char strings there.

Indexes and CHECK constraints listed here are mirrored 1:1 in the
Alembic migration `0001_initial.py` — keep them in sync.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Use Postgres JSONB in prod, fall back to generic JSON on SQLite (tests).
# `with_variant` ensures the same column declaration works in both.
JSONType = JSONB().with_variant(JSON(), "sqlite")

# UUIDs as native Postgres `uuid` type; SQLite gets a CHAR(36) under the hood
# via SQLAlchemy's generic UUID variant handling.
UUIDType = PG_UUID(as_uuid=True).with_variant(String(36), "sqlite")

# Server default for UUID PKs: works in Postgres after `pgcrypto` is enabled;
# on SQLite (tests) callers should pass an explicit `uuid4()` because SQLite
# has no `gen_random_uuid()` function.
_UUID_DEFAULT = text("gen_random_uuid()")


class User(Base):
    """A person who can log in and own workspaces."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_users_email", "email", unique=True),)


class Workspace(Base):
    """A folder/grouping for one or more database connections.

    Post-migration 0003, a Workspace no longer carries connection details
    itself — those live on :class:`WorkspaceConnection`. The legacy
    ``dialect`` / ``connection_meta`` columns are kept nullable for one
    release so a rollback can rebuild from them, but **new code must not
    read them**. They will be dropped in a follow-up migration.
    """

    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    owner_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # DEPRECATED — see class docstring. Do not read in new code.
    dialect: Mapped[str | None] = mapped_column(String(32), nullable=True)
    connection_meta: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True
    )
    # Status here is just an aggregate hint — the canonical per-DB
    # status lives on each WorkspaceConnection.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    owner: Mapped[User] = relationship(back_populates="workspaces")
    connections: Mapped[list["WorkspaceConnection"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Keep the legacy CHECK as None-tolerant so the nullable column
        # doesn't trip it. The new dialects live on WorkspaceConnection.
        CheckConstraint(
            "dialect IS NULL OR dialect IN ('postgres','sqlite','mysql',"
            "'clickhouse','oracle','mongodb','elasticsearch')",
            name="ck_workspaces_dialect",
        ),
        CheckConstraint(
            "status IN ('pending','profiling','ready','error','auth_error')",
            name="ck_workspaces_status",
        ),
        Index("ix_workspaces_owner_id", "owner_id"),
    )


class WorkspaceConnection(Base):
    """A single database connection inside a workspace.

    One workspace contains N of these. Each connection has its own
    dialect, connection metadata, encrypted credentials, profiling
    status, and schema bundle. The agent's executor is dispatched at
    the connection level — there's no cross-connection JOIN in this
    phase (that's the federation layer, coming later).
    """

    __tablename__ = "workspace_connections"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    workspace_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    dialect: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    workspace: Mapped[Workspace] = relationship(back_populates="connections")
    credentials: Mapped["WorkspaceCredentials | None"] = relationship(
        back_populates="connection",
        uselist=False,
        cascade="all, delete-orphan",
    )
    schema_bundle: Mapped["SchemaBundle | None"] = relationship(
        back_populates="connection",
        uselist=False,
        cascade="all, delete-orphan",
    )
    profile_jobs: Mapped[list["ProfileJob"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "dialect IN ('postgres','sqlite','mysql','clickhouse',"
            "'oracle','mongodb','elasticsearch')",
            name="ck_workspace_connections_dialect",
        ),
        CheckConstraint(
            "status IN ('pending','profiling','ready','error','auth_error')",
            name="ck_workspace_connections_status",
        ),
        UniqueConstraint(
            "workspace_id", "name", name="uq_workspace_connections_workspace_name"
        ),
        Index("ix_workspace_connections_workspace_id", "workspace_id"),
    )


class WorkspaceCredentials(Base):
    """AES-GCM-encrypted credentials for one WorkspaceConnection. PK == FK.

    Storing one row per connection (PK == FK) keeps the relationship 1:1
    at the schema level. `key_version` lets us rotate the master key.
    """

    __tablename__ = "workspace_credentials"

    connection_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspace_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    auth_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    connection: Mapped[WorkspaceConnection] = relationship(back_populates="credentials")

    __table_args__ = (
        CheckConstraint(
            "auth_kind IN ('password','dsn','iam','none')",
            name="ck_workspace_credentials_auth_kind",
        ),
    )


class SchemaBundle(Base):
    """Deterministically-profiled snapshot of one connection's schema.

    One row per WorkspaceConnection (PK == FK). The `bundle` JSON is the
    contract consumed by `agents/nodes/schema_loader` and friends; its
    structure is owned by `app/schemas/schema_bundle.py`.
    """

    __tablename__ = "schema_bundles"

    connection_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspace_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bundle: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ready'")
    )
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    connection: Mapped[WorkspaceConnection] = relationship(back_populates="schema_bundle")

    __table_args__ = (
        CheckConstraint(
            "status IN ('profiling','ready','stale','error')",
            name="ck_schema_bundles_status",
        ),
        Index("ix_schema_bundles_schema_hash", "schema_hash"),
    )


class ChatSession(Base):
    """One conversation thread inside a workspace.

    A session lives at the workspace level (so chat history is per
    workspace, not per connection) but **remembers the last connection
    used** in ``connection_id``. The chat UI surfaces a connection
    picker; the agent reads ``connection_id`` to pick the right engine.
    """

    __tablename__ = "chat_sessions"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    workspace_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Set to whichever connection the most recent turn ran against —
    # nullable because a session may exist before any turn has fired
    # (e.g., greeting / chitchat).
    connection_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("workspace_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workspace: Mapped[Workspace] = relationship(back_populates="chat_sessions")
    user: Mapped[User] = relationship(back_populates="chat_sessions")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    __table_args__ = (
        Index("ix_chat_sessions_workspace_id", "workspace_id"),
        Index("ix_chat_sessions_user_id", "user_id"),
    )


class Message(Base):
    """A single chat message. Assistant turns also carry a UISpec payload."""

    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    session_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Frontend/backend contract — see `app/schemas/ui_spec.py`.
    ui_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    session: Mapped[ChatSession] = relationship(back_populates="messages")
    query_history: Mapped["QueryHistory | None"] = relationship(
        back_populates="message",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','system')",
            name="ck_messages_role",
        ),
        Index("ix_messages_session_id_created_at", "session_id", "created_at"),
    )


class QueryHistory(Base):
    """Audit log: the exact SQL the agent generated for each assistant turn."""

    __tablename__ = "query_history"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    message_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    dialect: Mapped[str] = mapped_column(String(32), nullable=False)
    took_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    message: Mapped[Message] = relationship(back_populates="query_history")

    __table_args__ = (
        # Mirrors workspace_connections.dialect — see migration 0004.
        CheckConstraint(
            "dialect IN ('postgres','sqlite','mysql','clickhouse',"
            "'oracle','mongodb','elasticsearch')",
            name="ck_query_history_dialect",
        ),
        CheckConstraint(
            "status IN ('ok','validator_rejected','executor_error','timeout')",
            name="ck_query_history_status",
        ),
        Index("ix_query_history_message_id", "message_id"),
    )


class ProfileJob(Base):
    """Tracks a background schema-profiling run for one connection."""

    __tablename__ = "profile_jobs"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    connection_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspace_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'queued'")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    connection: Mapped[WorkspaceConnection] = relationship(back_populates="profile_jobs")

    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_profile_jobs_state",
        ),
        Index("ix_profile_jobs_connection_id", "connection_id"),
        Index("ix_profile_jobs_state", "state"),
    )


class Settings(Base):
    """Per-user preference key/value store.

    Kept narrow on purpose; not a generic config blob. Plug richer
    typed columns in here (e.g. `theme`, `default_workspace_id`) as
    product surface grows.
    """

    __tablename__ = "settings"

    user_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_settings_user_key"),
    )


class UploadedDocument(Base):
    """User-uploaded reference doc (markdown/plain text). Becomes RAG chunks.

    The original text is preserved so chunks can be re-built on demand (e.g.,
    after changing chunker parameters) without re-uploading.
    """

    __tablename__ = "uploaded_documents"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    owner_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_uploaded_documents_owner_id", "owner_id"),
        Index("ix_uploaded_documents_workspace_id", "workspace_id"),
    )


class RagChunk(Base):
    """A retrievable chunk in the RAG index.

    Kinds:
      - ``schema_table``  — one chunk per table (name + columns + FKs).
      - ``schema_column`` — one chunk per column (when high cardinality info).
      - ``api_endpoint``  — one chunk per FastAPI route in our own service.
      - ``user_doc``      — chunk of an uploaded document.

    ``embedding`` is a 1024-dim vector under Postgres (pgvector) and a JSON-
    encoded float array under SQLite (unit tests). Retrieval falls back to
    Python-side cosine similarity on the SQLite path.
    """

    __tablename__ = "rag_chunks"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    # workspace_id is nullable so global chunks (e.g., our REST API catalog)
    # can live in the same index. Workspace-scoped retrieval still filters
    # on it; connection_id narrows further when a specific DB is in play.
    workspace_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    connection_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("workspace_connections.id", ondelete="CASCADE"),
        nullable=True,
    )
    document_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("uploaded_documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # Column name "chunk_text" rather than "text" so it doesn't shadow the
    # imported SQL ``text()`` function in the class body below.
    chunk_text: Mapped[str] = mapped_column("text", Text, nullable=False)
    # Postgres: pgvector ``vector(1024)``; SQLite: JSON array of floats.
    # The actual column type is set in the migration so the model stays
    # dialect-agnostic. SQLAlchemy treats it as Text both sides via JSON.
    embedding: Mapped[Any] = mapped_column(JSONType, nullable=True)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, server_default=text("'{}'::jsonb")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('schema_table','schema_column','api_endpoint','user_doc')",
            name="ck_rag_chunks_kind",
        ),
        UniqueConstraint(
            "workspace_id", "kind", "source_key",
            name="uq_rag_chunks_workspace_kind_source",
        ),
        Index("ix_rag_chunks_workspace_id", "workspace_id"),
        Index("ix_rag_chunks_kind", "kind"),
        Index("ix_rag_chunks_document_id", "document_id"),
    )


__all__ = [
    "User",
    "Workspace",
    "WorkspaceConnection",
    "WorkspaceCredentials",
    "SchemaBundle",
    "ChatSession",
    "Message",
    "QueryHistory",
    "ProfileJob",
    "Settings",
    "UploadedDocument",
    "RagChunk",
]
