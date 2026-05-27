"""workspace can hold many database connections

Splits the old Workspace model in two:

  * **Workspace** — a folder/grouping. Owns name + owner. The legacy
    ``dialect`` and ``connection_meta`` columns are kept for one
    release so any rollback still works, but new code MUST NOT read
    them. They will be dropped in a follow-up migration.

  * **WorkspaceConnection** (new) — the actual database connection.
    One workspace has N connections, each with its own dialect,
    metadata, credentials, status, and profiled schema bundle.

Existing FK owners (WorkspaceCredentials, SchemaBundle, ProfileJob)
move to ``connection_id``. RagChunk and ChatSession keep their
workspace_id and gain an optional ``connection_id`` (the active one
when the session/chunk was created — null = workspace-level).

The data migration creates exactly one connection per existing
workspace, copies the old conn_meta + dialect + creds into it, then
rewires every dependent row.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-26 22:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    # ── 1) Create the new workspace_connections table ────────────────
    op.create_table(
        "workspace_connections",
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
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("dialect", sa.String(length=32), nullable=False),
        sa.Column(
            "connection_meta",
            postgresql.JSONB() if _is_postgres() else sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if _is_postgres() else sa.text("'{}'"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "dialect IN ('postgres','sqlite','mysql','clickhouse',"
            "'oracle','mongodb','elasticsearch')",
            name="ck_workspace_connections_dialect",
        ),
        sa.CheckConstraint(
            "status IN ('pending','profiling','ready','error','auth_error')",
            name="ck_workspace_connections_status",
        ),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_workspace_connections_workspace_name"
        ),
    )
    op.create_index(
        "ix_workspace_connections_workspace_id",
        "workspace_connections",
        ["workspace_id"],
    )

    # ── 2) Seed connection rows from existing workspaces ─────────────
    # One connection per workspace, named "default", carrying the
    # workspace's existing dialect + connection_meta. This preserves
    # all currently profiled bundles and chat history.
    if _is_postgres():
        op.execute(
            """
            INSERT INTO workspace_connections
                (id, workspace_id, name, dialect, connection_meta, status,
                 created_at, updated_at)
            SELECT gen_random_uuid(), id, 'default', dialect,
                   connection_meta, status, created_at, updated_at
            FROM workspaces
            """
        )
    else:
        # SQLite path (unit tests).
        op.execute(
            """
            INSERT INTO workspace_connections
                (id, workspace_id, name, dialect, connection_meta, status,
                 created_at, updated_at)
            SELECT lower(hex(randomblob(16))), id, 'default', dialect,
                   connection_meta, status, created_at, updated_at
            FROM workspaces
            """
        )

    # ── 3) workspace_credentials: re-aim FK at connection ───────────
    # We keep the old workspace_id column briefly so the data move is
    # safe, then drop it once connection_id is populated.
    op.add_column(
        "workspace_credentials",
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE workspace_credentials wc
        SET connection_id = (
            SELECT id FROM workspace_connections wsc
            WHERE wsc.workspace_id = wc.workspace_id
            LIMIT 1
        )
        """
    )
    # The old workspace_id column was the primary key — replace the PK
    # before we drop it.
    with op.batch_alter_table("workspace_credentials") as batch:
        batch.drop_constraint(
            "workspace_credentials_pkey"
            if _is_postgres()
            else "pk_workspace_credentials",
            type_="primary",
        ) if _is_postgres() else None
    if _is_postgres():
        op.execute("ALTER TABLE workspace_credentials DROP CONSTRAINT IF EXISTS workspace_credentials_pkey")
        op.execute("ALTER TABLE workspace_credentials DROP COLUMN workspace_id")
        op.execute("ALTER TABLE workspace_credentials ALTER COLUMN connection_id SET NOT NULL")
        op.execute(
            "ALTER TABLE workspace_credentials ADD CONSTRAINT workspace_credentials_pkey "
            "PRIMARY KEY (connection_id)"
        )
    else:
        # SQLite — recreate the table.
        op.execute("ALTER TABLE workspace_credentials RENAME TO workspace_credentials_old")
        op.create_table(
            "workspace_credentials",
            sa.Column(
                "connection_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workspace_connections.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("auth_kind", sa.String(length=32), nullable=False),
            sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
            sa.Column("nonce", sa.LargeBinary(), nullable=False),
            sa.Column(
                "key_version", sa.Integer(), nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "auth_kind IN ('password','dsn','iam','none')",
                name="ck_workspace_credentials_auth_kind",
            ),
        )
        op.execute(
            "INSERT INTO workspace_credentials "
            "(connection_id, auth_kind, ciphertext, nonce, key_version, created_at) "
            "SELECT connection_id, auth_kind, ciphertext, nonce, key_version, created_at "
            "FROM workspace_credentials_old"
        )
        op.execute("DROP TABLE workspace_credentials_old")

    # ── 4) schema_bundles: re-aim at connection ─────────────────────
    op.add_column(
        "schema_bundles",
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE schema_bundles sb
        SET connection_id = (
            SELECT id FROM workspace_connections wsc
            WHERE wsc.workspace_id = sb.workspace_id
            LIMIT 1
        )
        """
    )
    if _is_postgres():
        op.execute("ALTER TABLE schema_bundles DROP CONSTRAINT IF EXISTS schema_bundles_pkey")
        op.execute("ALTER TABLE schema_bundles DROP COLUMN workspace_id")
        op.execute("ALTER TABLE schema_bundles ALTER COLUMN connection_id SET NOT NULL")
        op.execute(
            "ALTER TABLE schema_bundles ADD CONSTRAINT schema_bundles_pkey "
            "PRIMARY KEY (connection_id)"
        )
    else:
        op.execute("ALTER TABLE schema_bundles RENAME TO schema_bundles_old")
        op.create_table(
            "schema_bundles",
            sa.Column(
                "connection_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workspace_connections.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "bundle",
                postgresql.JSONB() if _is_postgres() else sa.JSON(),
                nullable=False,
            ),
            sa.Column("schema_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'ready'"),
            ),
            sa.Column(
                "refreshed_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "status IN ('profiling','ready','stale','error')",
                name="ck_schema_bundles_status",
            ),
        )
        op.execute(
            "INSERT INTO schema_bundles "
            "(connection_id, bundle, schema_hash, status, refreshed_at) "
            "SELECT connection_id, bundle, schema_hash, status, refreshed_at "
            "FROM schema_bundles_old"
        )
        op.execute("DROP TABLE schema_bundles_old")

    # ── 5) profile_jobs: re-aim at connection ───────────────────────
    op.add_column(
        "profile_jobs",
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE profile_jobs pj
        SET connection_id = (
            SELECT id FROM workspace_connections wsc
            WHERE wsc.workspace_id = pj.workspace_id
            LIMIT 1
        )
        """
    )
    if _is_postgres():
        op.drop_index("ix_profile_jobs_workspace_id", table_name="profile_jobs")
        op.execute("ALTER TABLE profile_jobs DROP COLUMN workspace_id")
        op.execute("ALTER TABLE profile_jobs ALTER COLUMN connection_id SET NOT NULL")
        op.create_index(
            "ix_profile_jobs_connection_id", "profile_jobs", ["connection_id"]
        )

    # ── 6) chat_sessions: add optional connection_id ─────────────────
    op.add_column(
        "chat_sessions",
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Seed: default each existing session to its workspace's only connection.
    op.execute(
        """
        UPDATE chat_sessions cs
        SET connection_id = (
            SELECT id FROM workspace_connections wsc
            WHERE wsc.workspace_id = cs.workspace_id
            LIMIT 1
        )
        """
    )

    # ── 7) rag_chunks: add optional connection_id ────────────────────
    op.add_column(
        "rag_chunks",
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE rag_chunks rc
        SET connection_id = (
            SELECT id FROM workspace_connections wsc
            WHERE wsc.workspace_id = rc.workspace_id
            LIMIT 1
        )
        WHERE rc.workspace_id IS NOT NULL
        """
    )

    # ── 8) Workspaces: keep dialect/conn_meta columns nullable for back-compat ──
    # We do NOT drop them yet — that's a follow-up migration once all
    # readers are off them. Make them nullable so future inserts can
    # leave them empty.
    if _is_postgres():
        op.execute("ALTER TABLE workspaces ALTER COLUMN dialect DROP NOT NULL")
        op.execute("ALTER TABLE workspaces ALTER COLUMN connection_meta DROP NOT NULL")


def downgrade() -> None:
    # Down-migration is best-effort: we drop the new connection-scoped
    # bits and restore workspace_id columns from the surviving join.
    op.drop_column("rag_chunks", "connection_id")
    op.drop_column("chat_sessions", "connection_id")

    if _is_postgres():
        # profile_jobs
        op.add_column(
            "profile_jobs",
            sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.execute(
            "UPDATE profile_jobs pj SET workspace_id = "
            "(SELECT workspace_id FROM workspace_connections "
            " WHERE id = pj.connection_id)"
        )
        op.execute("ALTER TABLE profile_jobs ALTER COLUMN workspace_id SET NOT NULL")
        op.drop_index("ix_profile_jobs_connection_id", table_name="profile_jobs")
        op.drop_column("profile_jobs", "connection_id")
        op.create_index(
            "ix_profile_jobs_workspace_id", "profile_jobs", ["workspace_id"]
        )

        # schema_bundles
        op.execute("ALTER TABLE schema_bundles DROP CONSTRAINT schema_bundles_pkey")
        op.add_column(
            "schema_bundles",
            sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.execute(
            "UPDATE schema_bundles sb SET workspace_id = "
            "(SELECT workspace_id FROM workspace_connections "
            " WHERE id = sb.connection_id)"
        )
        op.execute("ALTER TABLE schema_bundles ALTER COLUMN workspace_id SET NOT NULL")
        op.execute(
            "ALTER TABLE schema_bundles ADD CONSTRAINT schema_bundles_pkey "
            "PRIMARY KEY (workspace_id)"
        )
        op.drop_column("schema_bundles", "connection_id")

        # workspace_credentials
        op.execute("ALTER TABLE workspace_credentials DROP CONSTRAINT workspace_credentials_pkey")
        op.add_column(
            "workspace_credentials",
            sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.execute(
            "UPDATE workspace_credentials wc SET workspace_id = "
            "(SELECT workspace_id FROM workspace_connections "
            " WHERE id = wc.connection_id)"
        )
        op.execute("ALTER TABLE workspace_credentials ALTER COLUMN workspace_id SET NOT NULL")
        op.execute(
            "ALTER TABLE workspace_credentials ADD CONSTRAINT workspace_credentials_pkey "
            "PRIMARY KEY (workspace_id)"
        )
        op.drop_column("workspace_credentials", "connection_id")

        # Make workspaces.dialect / connection_meta NOT NULL again
        op.execute("ALTER TABLE workspaces ALTER COLUMN dialect SET NOT NULL")
        op.execute("ALTER TABLE workspaces ALTER COLUMN connection_meta SET NOT NULL")

    op.drop_index(
        "ix_workspace_connections_workspace_id", table_name="workspace_connections"
    )
    op.drop_table("workspace_connections")
