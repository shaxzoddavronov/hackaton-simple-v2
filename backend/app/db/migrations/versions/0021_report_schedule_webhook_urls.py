"""add report_schedules.webhook_urls (Phase 33)

Webhook fan-out — a scheduled report can now POST its rendered payload
to Slack / Teams / Discord / Mattermost / custom incoming-webhook
endpoints in addition to delivering via email. Stored as a newline-
separated list of full URLs in a single TEXT column so the existing
``recipients`` UI shape (textarea) generalises trivially.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-02 09:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, Sequence[str], None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "report_schedules",
        sa.Column(
            "webhook_urls",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("report_schedules", "webhook_urls")
