"""Super-user administration endpoints (Phase 16).

Only callers with ``is_superuser=True`` reach any handler here —
guarded by :func:`require_superuser`. Three families:

  * ``POST /admin/users``                — create a user (only path
                                           that creates users now;
                                           public registration is gone)
  * ``GET  /admin/users``                — list users
  * ``GET  /admin/users/{id}``           — single user details
  * ``PATCH /admin/users/{id}``          — update is_active /
                                           is_superuser / password
  * ``DELETE /admin/users/{id}``         — hard delete; refresh tokens
                                           cascade
  * ``GET /admin/audit``                 — recent audit rows

Permission management endpoints (per-cluster / table / column grants)
land in the Phase 17 follow-up. The bones live here so the route
prefix and superuser-gating are in place.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password
from app.api.deps import require_superuser
from app.config import settings
from app.db.models import AuditLog, User
from app.db.session import get_db
from app.services.audit import log_action
from app.services.auth_tokens import revoke_all_for_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Schemas ──────────────────────────────────────────────────────


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)
    is_superuser: bool = False


class UpdateUserRequest(BaseModel):
    is_active: bool | None = None
    is_superuser: bool | None = None
    password: str | None = Field(default=None, min_length=4, max_length=128)
    email: EmailStr | None = None


class UserOut(BaseModel):
    id: str
    username: str
    # Plain ``str`` for the same reason MeResponse uses str — see
    # api/auth.py for the explainer. Write paths (CreateUserRequest /
    # UpdateUserRequest) keep ``EmailStr`` so new addresses are
    # validated strictly.
    email: str
    is_superuser: bool
    is_active: bool
    created_at: datetime


class AuditOut(BaseModel):
    id: str
    user_id: str | None
    action: str
    target_kind: str | None
    target_id: str | None
    status: Literal["ok", "error", "denied"]
    payload: dict[str, Any]
    client_ip: str | None
    user_agent: str | None
    created_at: datetime


# ── Password complexity ──────────────────────────────────────────


def _validate_password(pw: str) -> None:
    if len(pw) < settings.PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Password must be at least {settings.PASSWORD_MIN_LENGTH} "
                "characters"
            ),
        )
    if settings.PASSWORD_REQUIRE_DIGIT and not any(c.isdigit() for c in pw):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Password must contain at least one digit",
        )
    if settings.PASSWORD_REQUIRE_UPPER and not any(c.isupper() for c in pw):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Password must contain at least one uppercase letter",
        )


# ── Routes ───────────────────────────────────────────────────────


@router.post(
    "/users", response_model=UserOut, status_code=status.HTTP_201_CREATED
)
async def create_user(
    request: Request,
    payload: CreateUserRequest,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(require_superuser),
) -> UserOut:
    _validate_password(payload.password)
    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        is_superuser=payload.is_superuser,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # ``email`` and ``username`` are both unique; surface a
        # generic 409 so we don't leak which one collided.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username or email already in use",
        ) from exc

    await log_action(
        session,
        action="user.create",
        user_id=admin.id,
        target_kind="user",
        target_id=str(user.id),
        payload={
            "username": user.username,
            "is_superuser": user.is_superuser,
        },
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    await session.refresh(user)
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        is_superuser=user.is_superuser,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.get("/users", response_model=list[UserOut])
async def list_users(
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(require_superuser),  # noqa: ARG001
) -> list[UserOut]:
    rows = await session.execute(
        select(User).order_by(User.created_at.desc())
    )
    return [
        UserOut(
            id=str(u.id),
            username=u.username,
            email=u.email,
            is_superuser=u.is_superuser,
            is_active=u.is_active,
            created_at=u.created_at,
        )
        for u in rows.scalars().all()
    ]


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: UUID,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(require_superuser),  # noqa: ARG001
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        is_superuser=user.is_superuser,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    request: Request,
    user_id: UUID,
    payload: UpdateUserRequest,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(require_superuser),
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")

    changes: dict[str, Any] = {}
    if payload.password is not None:
        _validate_password(payload.password)
        user.password_hash = hash_password(payload.password)
        # Revoke every existing refresh token on password change so
        # a stolen session can't outlive the password rotation.
        await revoke_all_for_user(session, user.id)
        changes["password"] = "rotated"
    if payload.is_active is not None and payload.is_active != user.is_active:
        # Self-deactivation guard — keeps the admin from locking out
        # of their own session and creating a one-superuser-loss
        # situation. The frontend should also gray out the toggle on
        # the calling admin's row.
        if user.id == admin.id and not payload.is_active:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="cannot deactivate your own account",
            )
        user.is_active = payload.is_active
        changes["is_active"] = payload.is_active
        if not payload.is_active:
            await revoke_all_for_user(session, user.id)
    if (
        payload.is_superuser is not None
        and payload.is_superuser != user.is_superuser
    ):
        # Same idea — don't let the last admin demote themselves.
        if user.id == admin.id and not payload.is_superuser:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="cannot demote your own account",
            )
        user.is_superuser = payload.is_superuser
        changes["is_superuser"] = payload.is_superuser
    if payload.email is not None and payload.email != user.email:
        user.email = payload.email
        changes["email"] = payload.email

    if changes:
        await log_action(
            session,
            action="user.update",
            user_id=admin.id,
            target_kind="user",
            target_id=str(user.id),
            payload=changes,
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    await session.commit()
    await session.refresh(user)
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        is_superuser=user.is_superuser,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.delete(
    "/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_user(
    request: Request,
    user_id: UUID,
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(require_superuser),
) -> None:
    if user_id == admin.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="cannot delete your own account",
        )
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    await session.delete(user)
    await log_action(
        session,
        action="user.delete",
        user_id=admin.id,
        target_kind="user",
        target_id=str(user_id),
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()


@router.get("/audit", response_model=list[AuditOut])
async def list_audit(
    session: AsyncSession = Depends(get_db),
    admin: User = Depends(require_superuser),  # noqa: ARG001
    limit: int = Query(default=100, ge=1, le=1000),
    user_id: UUID | None = Query(default=None),
    action: str | None = Query(default=None, max_length=64),
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[AuditOut]:
    """Read the audit log. Default 100 most-recent rows. Filterable by
    user, action prefix, and status (ok/error/denied)."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if status_filter in ("ok", "error", "denied"):
        stmt = stmt.where(AuditLog.status == status_filter)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        AuditOut(
            id=str(r.id),
            user_id=str(r.user_id) if r.user_id else None,
            action=r.action,
            target_kind=r.target_kind,
            target_id=r.target_id,
            status=r.status,  # type: ignore[arg-type]
            payload=dict(r.payload or {}),
            client_ip=r.client_ip,
            user_agent=r.user_agent,
            created_at=r.created_at,
        )
        for r in rows
    ]
