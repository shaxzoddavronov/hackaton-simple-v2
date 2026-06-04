"""Authentication endpoints (Phase 16).

Routes:
  * ``POST /auth/login``    — username OR email + password →
                              ``{access_token, refresh_token, token_type}``
  * ``POST /auth/refresh``  — single-use refresh → new access + new
                              refresh (rotated)
  * ``POST /auth/logout``   — revoke a refresh token (or all of the
                              caller's tokens with ``all=True``)
  * ``GET  /auth/me``       — current user (id, username, email, role)

Public ``/auth/register`` is intentionally absent. Phase 16 made user
creation a super-user-only action; see ``api/admin.py``. A bootstrap
super-user is seeded from ``QM_BOOTSTRAP_SUPERUSER_*`` env vars on
first startup when no super-user exists.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import create_access_token, get_current_user
from app.db.models import RefreshToken, User
from app.db.session import get_db
from app.limiter import limiter
from app.services.audit import log_action
from app.services.auth_tokens import (
    consume_refresh_token,
    issue_refresh_token,
    revoke_all_for_user,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# bcrypt — same context used for both new hashes and verification of
# the existing email-only schema (passlib auto-detects).
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Schemas ──────────────────────────────────────────────────────


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # access-token seconds-until-expiry


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=8, max_length=200)


class LogoutRequest(BaseModel):
    # Optional — if omitted with all=False we just no-op (the access
    # token itself doesn't need server-side revocation).
    refresh_token: str | None = Field(default=None, max_length=200)
    all: bool = False


class MeResponse(BaseModel):
    id: str
    username: str
    # ``str`` (not ``EmailStr``) so reads succeed even for emails that
    # Pydantic v2's strict validator now rejects (``.local`` /
    # ``.test`` / etc. — flagged as "special-use" since email-validator
    # 2.x). Write paths (admin create/update) still use ``EmailStr``
    # so new addresses meet the strict shape.
    email: str
    is_superuser: bool
    is_active: bool


# ── Helpers ──────────────────────────────────────────────────────


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ua = request.headers.get("user-agent")
    ip = request.client.host if request.client else None
    return ua, ip


async def _resolve_login_identifier(
    session: AsyncSession, identifier: str
) -> User | None:
    """Accept either username or email in the same ``username`` form
    field. Phase 16 kept the OAuth2PasswordRequestForm shape so
    Swagger UI's password-grant flow keeps working — we just look up
    by both columns."""
    ident = identifier.strip()
    if not ident:
        return None
    if "@" in ident:
        row = await session.execute(
            select(User).where(User.email == ident)
        )
    else:
        row = await session.execute(
            select(User).where(User.username == ident)
        )
    return row.scalar_one_or_none()


# ── Routes ───────────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,  # slowapi requires Request first
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_db),
) -> TokenResponse:
    user = await _resolve_login_identifier(session, form.username)

    # Constant-ish-time failure: run the hash compare against a dummy
    # hash when the user doesn't exist, so timing doesn't leak which
    # accounts are valid.
    if user is None or not user.is_active:
        # Audit anonymous failed login.
        await log_action(
            session,
            action="auth.login",
            target_kind="username",
            target_id=form.username[:64] if form.username else None,
            status="denied",
            payload={"reason": "unknown_or_inactive"},
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not _pwd.verify(form.password, user.password_hash):
        await log_action(
            session,
            action="auth.login",
            user_id=user.id,
            status="denied",
            payload={"reason": "bad_password"},
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    ua, ip = _client_meta(request)
    refresh_raw, _row = await issue_refresh_token(
        session, user=user, user_agent=ua, client_ip=ip
    )
    access = create_access_token(str(user.id))
    from app.config import settings  # local import to keep top tight

    await log_action(
        session,
        action="auth.login",
        user_id=user.id,
        status="ok",
        client_ip=ip,
        user_agent=ua,
    )
    await session.commit()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh_raw,
        expires_in=settings.JWT_EXPIRES_MIN * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("20/minute")
async def refresh(
    request: Request,
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Single-use refresh: consume + rotate. A replay (token used
    twice) hard-fails — the legit holder's next refresh would have
    cleaned it up; an attacker is locked out."""
    try:
        row: RefreshToken = await consume_refresh_token(
            session, payload.refresh_token
        )
    except ValueError as e:
        await log_action(
            session,
            action="auth.refresh",
            status="denied",
            payload={"reason": str(e)},
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        await log_action(
            session,
            action="auth.refresh",
            user_id=row.user_id,
            status="denied",
            payload={"reason": "user_missing_or_inactive"},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer active",
        )

    ua, ip = _client_meta(request)
    new_refresh, _new_row = await issue_refresh_token(
        session, user=user, user_agent=ua, client_ip=ip
    )
    access = create_access_token(str(user.id))
    from app.config import settings

    await log_action(
        session,
        action="auth.refresh",
        user_id=user.id,
        status="ok",
        client_ip=ip,
        user_agent=ua,
    )
    await session.commit()
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.JWT_EXPIRES_MIN * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    payload: LogoutRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a refresh token. With ``all=True`` revokes every active
    refresh for the caller (logout-everywhere). The access token
    itself isn't tracked — it expires on its own within
    ``JWT_EXPIRES_MIN`` minutes."""
    revoked = 0
    if payload.all:
        revoked = await revoke_all_for_user(session, user.id)
    elif payload.refresh_token:
        try:
            await consume_refresh_token(session, payload.refresh_token)
            revoked = 1
        except ValueError:
            # Idempotent: logging out an unknown / expired token is a
            # no-op success from the client's perspective.
            revoked = 0

    ua, ip = _client_meta(request)
    await log_action(
        session,
        action="auth.logout",
        user_id=user.id,
        status="ok",
        payload={"revoked": revoked, "all": payload.all},
        client_ip=ip,
        user_agent=ua,
    )
    await session.commit()


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        id=str(user.id),
        username=user.username,
        email=user.email,
        is_superuser=user.is_superuser,
        is_active=user.is_active,
    )


# Password hashing helper re-exported so admin.py uses the same
# context (bcrypt rounds, deprecated-scheme handling).
def hash_password(plaintext: str) -> str:
    return _pwd.hash(plaintext)
