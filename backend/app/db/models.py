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
    """A person who can log in and own workspaces.

    Phase 16 added ``username`` as a primary login identifier alongside
    ``email``. Both columns are unique; ``/auth/login`` accepts either
    in the ``username`` form field.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("TRUE")
    )
    # Phase 16: super-users are the only role that can register new
    # users, change roles, and edit permissions (see api/admin.py).
    # The very first user is promoted on startup via the
    # ``QM_BOOTSTRAP_SUPERUSER_*`` env vars (config.py).
    is_superuser: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("FALSE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
        Index("ix_users_username", "username", unique=True),
    )


class RefreshToken(Base):
    """Server-stored refresh token issued at login.

    Why server-side instead of self-contained JWT? Refresh tokens need
    to be revocable — on logout, on password change, on suspicious
    activity. A stateless JWT can't be revoked without a deny-list,
    and a deny-list is just a server-stored table that we already
    have. We store only the hash so a DB leak can't replay sessions.

    Lifecycle:
      * Issued at ``POST /auth/login`` — random 32 bytes, base64-url
        encoded; client gets the raw token, DB stores SHA-256.
      * Spent at ``POST /auth/refresh`` — single-use: row is marked
        ``revoked_at`` and a new pair (access + refresh) is minted
        (rotation). If a stolen-token replay arrives, the second use
        fails and the attacker is locked out.
      * Revoked at ``POST /auth/logout`` — same flag, no new pair.
      * Expires at ``expires_at`` — clean-up via a periodic sweep
        (cron / Celery beat). Stale rows are harmless until reused.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    user_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SHA-256 hex of the raw token. 64 chars.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Free-form optional UA / IP for auditing — we don't enforce a
    # session model, this is observability only.
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_token_hash", "token_hash", unique=True),
    )


class AuditLog(Base):
    """Append-only audit trail of user actions.

    Phase 16. Writes happen via :mod:`services.audit` from FastAPI
    middleware (per-request) and from explicit ``log_action`` calls
    inside high-value handlers (user create, permission change,
    chat turn, doc-source crawl, …).

    Schema is deliberately wide and loose:
      * ``user_id`` nullable — anonymous requests (login attempts) and
        system tasks (Celery beat) leave it blank.
      * ``action`` is a short snake_case key like ``user.create``,
        ``permission.grant``, ``chat.turn``, ``auth.login``.
      * ``target_kind`` + ``target_id`` localize WHAT was acted on —
        e.g. ``("user", <uuid>)`` or ``("connection", <uuid>)``.
      * ``payload`` is a free-form JSON dict for action-specific
        details (what fields changed, error code, etc.). Bounded by
        application code; the column is unindexed.
      * ``status`` — ``ok`` / ``error`` / ``denied`` so audit consumers
        can filter for failures.

    No FK constraints on ``target_id`` — referenced rows may be
    deleted (data retention) without orphaning the audit row. We
    accept stale pointers as a feature: the log preserves history.
    """

    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    user_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    target_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'ok'")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, server_default=text("'{}'::jsonb")
    )
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ok','error','denied')",
            name="ck_audit_log_status",
        ),
        Index("ix_audit_log_user_id", "user_id"),
        Index("ix_audit_log_action", "action"),
        Index("ix_audit_log_created_at", "created_at"),
    )


class Workspace(Base):
    """A folder/grouping for one or more database connections.

    Connection details (dialect, metadata, credentials) live on
    :class:`WorkspaceConnection`. Migration 0003 split them out; the
    legacy ``dialect`` / ``connection_meta`` columns lingered as
    nullable shadows through migration 0005; migration 0006 dropped
    them. ``status`` here remains as an aggregate hint — the
    canonical per-DB status lives on each WorkspaceConnection.
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
            "'oracle','mongodb','elasticsearch','duckdb','mssql',"
            "'rest_api','snowflake')",
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
        # Mirrors workspace_connections.dialect — see migrations 0004/0005/0007.
        CheckConstraint(
            "dialect IN ('postgres','sqlite','mysql','clickhouse',"
            "'oracle','mongodb','elasticsearch','duckdb','mssql',"
            "'rest_api','snowflake')",
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
            "kind IN ('schema_table','schema_column','api_endpoint',"
            "'user_doc','harvested_doc')",
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


class DocSource(Base):
    """Registered crawl source for documents harvested into the RAG index.

    Three source kinds (one DocSource row per registered source):
      - ``folder``     — server-local folder path; walked recursively.
      - ``url_list``   — explicit list of URLs to fetch.
      - ``db_column``  — values pulled from a WorkspaceConnection by
                          SELECTing a column, then each value is treated
                          as a URL or filesystem path and fetched.

    The harvester (services/rag/doc_harvest.py) reads ``config`` based
    on ``source_kind`` and produces (filename, bytes) tuples for the
    extractor. Chunks land in ``rag_chunks`` with
    ``kind='harvested_doc'`` and ``source_key`` of the form
    ``"docsource:<id>:<filename>"``.
    """

    __tablename__ = "doc_sources"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    workspace_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Per-kind config:
    #   folder    : {path: "/abs/dir", recursive: true,
    #                extensions: [".pdf", ".docx"]}
    #   url_list  : {urls: ["https://...", ...]}
    #   db_column : {connection_id: "...", table: "documents",
    #                column: "file_url", url_prefix: "https://..."}
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'idle'")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_harvested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    doc_count: Mapped[int] = mapped_column(
        nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('folder','url_list','db_column','smb','gdrive','onedrive','imap','slack','telegram')",
            name="ck_doc_sources_kind",
        ),
        CheckConstraint(
            "status IN ('idle','harvesting','ready','error')",
            name="ck_doc_sources_status",
        ),
        UniqueConstraint(
            "workspace_id", "name", name="uq_doc_sources_workspace_name"
        ),
        Index("ix_doc_sources_workspace_id", "workspace_id"),
    )


class Dashboard(Base):
    """A user-curated collection of saved questions.

    Phase 26 — once the user has a question they want to keep an
    eye on ("refund queue today", "top customers this week"), they
    star the message in the chat. The agent transcribes the
    question into a SavedQuestion row and groups it under a
    Dashboard. The dashboard page re-runs every question on demand
    so the user has a snapshot view without re-typing the prompt.
    """

    __tablename__ = "dashboards"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    owner_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "name", name="uq_dashboards_workspace_name"
        ),
        Index("ix_dashboards_owner_id", "owner_id"),
        Index("ix_dashboards_workspace_id", "workspace_id"),
    )


class ReportSchedule(Base):
    """A periodic email summary of a Dashboard's saved questions.

    Phase 29 — admin "stars" a dashboard for daily / weekly / cron
    delivery. The schedule's owner_id receives the email; multiple
    extra recipients can be CC'd via ``recipients`` (comma-separated
    addresses). At each fire the Celery beat dispatcher re-runs every
    saved question in the dashboard, renders an HTML digest, and
    delivers via SMTP.
    """

    __tablename__ = "report_schedules"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    owner_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    dashboard_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("dashboards.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Standard 5-field cron expression: ``minute hour dom month dow``.
    # Validated at API layer with the croniter package.
    cron: Mapped[str] = mapped_column(String(64), nullable=False)
    # CSV of email addresses beyond the owner. Empty string = owner only.
    recipients: Mapped[str] = mapped_column(
        String(2048), nullable=False, server_default=text("''")
    )
    enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_report_schedules_workspace_id", "workspace_id"),
        Index("ix_report_schedules_dashboard_id", "dashboard_id"),
        Index("ix_report_schedules_enabled", "enabled"),
    )


class SavedQuestion(Base):
    """A natural-language question the user starred for re-running.

    Stores the prompt + the connection it should run against. The
    dashboard page POSTs each row through the normal /chat pipeline
    on render so the result is always fresh (subject to the Phase
    23 query-result cache hit-rate).

    The dashboard_id is nullable — starred-but-not-yet-grouped
    questions live in an implicit "Inbox" until the user files them.
    """

    __tablename__ = "saved_questions"

    id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True, server_default=_UUID_DEFAULT
    )
    owner_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    dashboard_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("dashboards.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The connection the saved question should run against. We pin
    # this at save time (rather than re-resolving via the
    # workspace_resolver each rerun) so a dashboard view is
    # deterministic — moving connections between workspaces won't
    # silently change what the saved question targets.
    connection_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("workspace_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # ``position`` lets the frontend order cards inside a dashboard
    # without a separate ordering table. NULL means "append".
    position: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_saved_questions_owner_id", "owner_id"),
        Index("ix_saved_questions_workspace_id", "workspace_id"),
        Index("ix_saved_questions_dashboard_id", "dashboard_id"),
    )


__all__ = [
    "User",
    "RefreshToken",
    "AuditLog",
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
    "DocSource",
    "Dashboard",
    "SavedQuestion",
    "ReportSchedule",
]
