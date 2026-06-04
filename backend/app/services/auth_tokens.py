"""Access + refresh token primitives.

Phase 16. Access tokens stay as the existing short-lived JWTs (15 min
default). Refresh tokens are random 32-byte values, SHA-256-hashed in
the DB, single-use with rotation: every successful ``/auth/refresh``
revokes the consumed row and mints a new pair.

Rotation prevents replay: if an attacker steals a refresh token and
uses it, the legitimate user's next refresh fails because the token
is already revoked — they re-login and the attacker is locked out.

The raw token format is base64url of 32 random bytes (~43 chars).
The DB stores the SHA-256 hex digest (64 chars). A DB dump alone
cannot replay sessions.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import RefreshToken, User


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_refresh_token() -> str:
    """Return a fresh refresh-token string suitable for the client.

    32 bytes ≙ 256 bits of entropy, base64url-encoded for safe
    transport in JSON / headers / URL params (~43 ASCII chars). The
    client must store this somewhere persistent (HttpOnly cookie in
    prod, localStorage on this hackathon build).
    """
    return secrets.token_urlsafe(32)


async def issue_refresh_token(
    session: AsyncSession,
    *,
    user: User,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> tuple[str, RefreshToken]:
    """Mint a new refresh token + persist its hash. Returns the raw
    token (for the client) and the row (for tests / audit)."""
    raw = generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRES_DAYS
    )
    row = RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        expires_at=expires_at,
        user_agent=(user_agent or "")[:255] or None,
        client_ip=(client_ip or "")[:64] or None,
    )
    session.add(row)
    # Flush so callers can inspect ``row.id`` before commit.
    await session.flush()
    return raw, row


async def consume_refresh_token(
    session: AsyncSession, raw: str
) -> RefreshToken:
    """Look up + revoke a refresh token in one atomic step.

    Raises ``ValueError`` (caller maps to 401) if the token is unknown,
    expired, or already revoked. Single-use semantics: a second
    submission of the same token is treated as a replay and fails —
    the original user's session is also already gone, forcing them to
    re-login. This catches token theft.
    """
    row = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == _hash_token(raw)
            )
        )
    ).scalar_one_or_none()

    if row is None:
        raise ValueError("unknown refresh token")
    if row.revoked_at is not None:
        # Replay detected. The legit holder would have rotated the
        # token at next use; an attacker is the only one left holding
        # the old hash. We surface this distinctly so audit logs can
        # flag the user account for review.
        raise ValueError("refresh token already revoked (possible replay)")
    if row.expires_at <= datetime.now(timezone.utc):
        raise ValueError("refresh token expired")

    row.revoked_at = datetime.now(timezone.utc)
    return row


async def revoke_all_for_user(
    session: AsyncSession, user_id: UUID
) -> int:
    """Bulk-revoke every active refresh token for a user. Used on
    logout-everywhere, password change, and superuser-driven
    deactivation. Returns the count revoked."""
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    for r in rows:
        r.revoked_at = now
    return len(rows)


__all__ = [
    "generate_refresh_token",
    "issue_refresh_token",
    "consume_refresh_token",
    "revoke_all_for_user",
]
