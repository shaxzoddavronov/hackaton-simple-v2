"""Phase 16: username + is_superuser + refresh_tokens + audit_log

Four schema changes:

  1. ``users.username`` (NEW)  — separate login identifier. Backfilled
     from the email local-part, collisions suffixed with a slice of
     the user id.
  2. ``users.is_active``       — soft-disable flag, defaults TRUE.
  3. ``users.is_superuser``    — role bit; only super-users may add
     other users and edit permissions (Phase 16).
  4. ``refresh_tokens``        — server-stored, revocable refresh
     tokens with token_hash + expires_at + revoked_at. Single-use
     rotation; logout marks revoked.
  5. ``audit_log``             — append-only action history; the
     Phase 16 audit middleware writes here.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-28 14:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    # 1) Add columns as nullable so existing rows survive the migration.
    op.add_column(
        "users",
        sa.Column("username", sa.String(64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_superuser",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )

    # 2) Backfill username from the email local-part. Duplicate
    #    local-parts collide on the unique index, so we suffix them
    #    with ``_<short-id>`` derived from the user id. This keeps the
    #    backfill deterministic and the migration idempotent on retry.
    if _is_postgres():
        op.execute(
            """
            UPDATE users
            SET username = sub.uname
            FROM (
              SELECT id,
                     CASE
                       WHEN COUNT(*) OVER (PARTITION BY split_part(email,'@',1)) > 1
                       THEN split_part(email,'@',1)
                            || '_'
                            || substr(REPLACE(CAST(id AS text),'-',''), 1, 6)
                       ELSE split_part(email,'@',1)
                     END AS uname
              FROM users
            ) sub
            WHERE users.id = sub.id
              AND users.username IS NULL
            """
        )
    else:
        # SQLite — used by unit tests with empty users tables anyway.
        op.execute(
            """
            UPDATE users
            SET username = COALESCE(username,
                substr(email, 1, instr(email||'@','@')-1))
            WHERE username IS NULL
            """
        )

    # 3) Promote username to NOT NULL + UNIQUE now that every row has one.
    op.alter_column("users", "username", nullable=False)
    op.create_index(
        "ix_users_username", "users", ["username"], unique=True
    )

    # 4) refresh_tokens table.
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36),
            primary_key=True,
            server_default=sa.text(
                "gen_random_uuid()" if _is_postgres() else "(lower(hex(randomblob(16))))"
            ),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"]
    )
    op.create_index(
        "ix_refresh_tokens_token_hash",
        "refresh_tokens",
        ["token_hash"],
        unique=True,
    )

    # 5) audit_log table. Append-only; the audit middleware writes
    #    here on every authenticated request + explicit log_action
    #    calls from handlers that matter (user create, permission
    #    change, chat turn, etc.).
    json_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if _is_postgres()
        else sa.JSON()
    )
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36),
            primary_key=True,
            server_default=sa.text(
                "gen_random_uuid()" if _is_postgres() else "(lower(hex(randomblob(16))))"
            ),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'ok'"),
        ),
        sa.Column(
            "payload",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb" if _is_postgres() else "'{}'"),
        ),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('ok','error','denied')",
            name="ck_audit_log_status",
        ),
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(
        "ix_refresh_tokens_token_hash", table_name="refresh_tokens"
    )
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_column("users", "is_superuser")
    op.drop_column("users", "is_active")
    op.drop_column("users", "username")
