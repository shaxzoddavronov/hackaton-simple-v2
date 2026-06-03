"""add chat_sessions.summary jsonb (Phase 36)

Long-running chat sessions blow the LLM context budget. Phase 36
periodically replaces the OLDEST half of a session's history with a
single LLM-generated summary, persisted on the session row so a
re-rendered chat reads back the same context the agent sees.

Stored shape::

    {
      "text": "the user has been auditing user-quiz performance ...",
      "through_message_id": "<uuid>",
      "updated_at": "2026-06-04T00:00:00+00:00"
    }

When NULL, no summary exists yet — the agent operates on raw
messages as before.

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-04 00:10:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: Union[str, Sequence[str], None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    json_col = postgresql.JSONB() if _is_postgres() else sa.JSON()
    op.add_column(
        "chat_sessions",
        sa.Column("summary", json_col, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "summary")
