"""Shared FastAPI dependencies — auth in particular.

Owns the OAuth2 password-bearer scheme, the JWT decoder, and the token
factory. The actual ``/auth/login`` and ``/auth/register`` routes land
in a later wave; this module is intentionally route-free so it can be
imported from any router without circular import risk.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import User
from app.db.session import get_db

# `tokenUrl` is the route Swagger UI uses to obtain a token; the route
# itself doesn't exist yet, which is fine — FastAPI only treats this as
# a documentation hint.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def create_access_token(sub: str, expires_minutes: int | None = None) -> str:
    """Mint a signed JWT carrying ``sub`` (typically the user UUID string).

    Encodes ``iat`` and ``exp`` claims; uses ``Settings.JWT_ALG`` so the
    algorithm is configurable without code changes.
    """
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=expires_minutes or settings.JWT_EXPIRES_MIN)
    payload: dict[str, object] = {
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def _decode_token(token: str) -> str:
    """Return the ``sub`` claim from a valid token or raise 401."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALG],
        )
    except JWTError as exc:  # noqa: BLE001 — narrow upstream
        raise _CREDENTIALS_EXCEPTION from exc

    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise _CREDENTIALS_EXCEPTION
    return sub


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the authenticated user or raise 401.

    Steps: decode token -> parse UUID -> fetch user -> assert exists +
    active. Phase 16: deactivated accounts (``is_active=False``) are
    rejected even with a valid signed token, so disable-via-admin
    takes effect at the next request without revoking each refresh
    token by hand.

    Each failure collapses into the same 401 so the client cannot
    enumerate which check failed.
    """
    sub = _decode_token(token)
    try:
        user_id = UUID(sub)
    except ValueError as exc:
        raise _CREDENTIALS_EXCEPTION from exc

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise _CREDENTIALS_EXCEPTION
    return user


async def require_superuser(
    user: User = Depends(get_current_user),
) -> User:
    """Gate for admin-only endpoints (Phase 16).

    Returns 403 (not 401) when the caller is authenticated but lacks
    the role — distinguishing auth from authorization helps the
    frontend route the user to a "permission denied" UI instead of
    the login page.
    """
    if not user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="superuser privileges required",
        )
    return user


__all__ = [
    "oauth2_scheme",
    "create_access_token",
    "get_current_user",
    "require_superuser",
]
