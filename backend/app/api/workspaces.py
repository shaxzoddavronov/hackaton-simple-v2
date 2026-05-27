"""Workspaces + WorkspaceConnections REST API.

A Workspace is a folder (name + owner). A WorkspaceConnection is the
actual database link living inside it. Endpoints:

  /workspaces                                       — list / create folders
  /workspaces/{id}                                  — get / delete one
  /workspaces/{id}/connections                      — list / add a DB to it
  /workspaces/{id}/connections/{cid}                — get / delete
  /workspaces/{id}/connections/{cid}/test           — probe (no insert)
  /workspaces/{id}/connections/{cid}/refresh        — re-profile schema
  /workspaces/test-connection                       — probe without saving
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import (
    ProfileJob,
    User,
    Workspace,
    WorkspaceConnection,
    WorkspaceCredentials,
)
from app.db.session import get_db
from app.engines.registry import get_engine
from app.services import crypto

log = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

# How long we let a connection probe run before declaring it dead.
_CONNECTION_TEST_TIMEOUT_S = 8.0

_DIALECTS = Literal[
    "postgres", "sqlite", "mysql", "clickhouse", "oracle",
    "mongodb", "elasticsearch",
]


# ── Schemas ──────────────────────────────────────────────────────────


class CreateWorkspaceRequest(BaseModel):
    """Folder-level creation. No connection details — those land via
    ``POST /workspaces/{id}/connections`` after this returns."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    status: str
    connection_count: int


class ConnectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    dialect: _DIALECTS
    connection_meta: dict[str, Any] = Field(default_factory=dict)
    auth_kind: Literal["password", "dsn", "iam", "none"] = "password"
    credentials: dict[str, str] = Field(default_factory=dict)


class ConnectionOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    dialect: str
    status: str
    profile_job_id: str | None = None


class TestConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dialect: _DIALECTS
    connection_meta: dict[str, Any] = Field(default_factory=dict)
    auth_kind: Literal["password", "dsn", "iam", "none"] = "password"
    credentials: dict[str, str] = Field(default_factory=dict)


class TestConnectionResult(BaseModel):
    ok: bool
    dialect: str | None = None
    table_count: int | None = None
    table_names_preview: list[str] | None = None
    error: str | None = None
    error_kind: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────


async def _probe_connection(
    dialect: str,
    connection_meta: dict[str, Any],
    credentials: dict[str, str],
) -> TestConnectionResult:
    fake = SimpleNamespace(
        dialect=dialect,
        connection_meta=connection_meta,
        _credentials=credentials,
    )
    qe = None
    try:
        qe = get_engine(fake)
    except ValueError as e:
        return TestConnectionResult(ok=False, error=str(e), error_kind="config")
    except Exception as e:  # pragma: no cover
        return TestConnectionResult(ok=False, error=str(e), error_kind="other")

    try:
        bundle = await asyncio.wait_for(
            qe.introspect_schema(), timeout=_CONNECTION_TEST_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return TestConnectionResult(
            ok=False,
            error=f"connection probe exceeded {_CONNECTION_TEST_TIMEOUT_S:.0f}s",
            error_kind="timeout",
        )
    except Exception as e:
        msg = str(e)
        kind = "other"
        lower = msg.lower()
        if any(s in lower for s in ("password", "auth", "permission denied", "role")):
            kind = "auth"
        elif any(
            s in lower
            for s in (
                "connection refused", "could not connect", "host",
                "name or service", "unreachable", "getaddrinfo",
                "dns", "no such host", "network is unreachable",
            )
        ):
            kind = "network"
        return TestConnectionResult(ok=False, error=msg, error_kind=kind)
    finally:
        if qe is not None:
            try:
                await qe.aclose()
            except Exception:  # pragma: no cover
                pass

    qnames = [f"{t.schema}.{t.name}" for t in bundle.tables]
    return TestConnectionResult(
        ok=True,
        dialect=bundle.dialect,
        table_count=len(qnames),
        table_names_preview=qnames[:10],
    )


def _enqueue_profile_job(connection_id: str, profile_job_id: str) -> None:
    """Enqueue Celery profile task. Isolated so tests can monkeypatch."""
    from app.workers.profile_task import run_profile_job

    run_profile_job.delay(connection_id, profile_job_id)


async def _get_owned_workspace(
    session: AsyncSession, workspace_id: UUID, user: User
) -> Workspace:
    ws = await session.get(Workspace, workspace_id)
    if ws is None or ws.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return ws


async def _get_owned_connection(
    session: AsyncSession,
    workspace_id: UUID,
    connection_id: UUID,
    user: User,
) -> WorkspaceConnection:
    await _get_owned_workspace(session, workspace_id, user)
    conn = await session.get(WorkspaceConnection, connection_id)
    if conn is None or conn.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return conn


# ── Workspace endpoints ──────────────────────────────────────────────


@router.post(
    "",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    payload: CreateWorkspaceRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceOut:
    ws = Workspace(
        owner_id=current_user.id,
        name=payload.name,
        dialect=None,
        connection_meta=None,
        status="pending",
    )
    session.add(ws)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A workspace with that name already exists",
        ) from exc
    await session.refresh(ws)
    return WorkspaceOut(
        id=str(ws.id), name=ws.name, status=ws.status, connection_count=0
    )


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[WorkspaceOut]:
    rows = await session.execute(
        select(Workspace)
        .where(Workspace.owner_id == current_user.id)
        .order_by(Workspace.created_at.desc())
    )
    out: list[WorkspaceOut] = []
    for w in rows.scalars().all():
        # Count connections per workspace. N+1 here is fine — workspaces
        # are typically <100 per user.
        cnt_rows = await session.execute(
            select(WorkspaceConnection.id).where(
                WorkspaceConnection.workspace_id == w.id
            )
        )
        cnt = len(cnt_rows.scalars().all())
        out.append(
            WorkspaceOut(
                id=str(w.id), name=w.name, status=w.status, connection_count=cnt
            )
        )
    return out


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceOut:
    ws = await _get_owned_workspace(session, workspace_id, current_user)
    cnt_rows = await session.execute(
        select(WorkspaceConnection.id).where(
            WorkspaceConnection.workspace_id == ws.id
        )
    )
    cnt = len(cnt_rows.scalars().all())
    return WorkspaceOut(
        id=str(ws.id), name=ws.name, status=ws.status, connection_count=cnt
    )


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    ws = await _get_owned_workspace(session, workspace_id, current_user)
    await session.delete(ws)
    await session.commit()


# ── Connection endpoints ─────────────────────────────────────────────


@router.post(
    "/test-connection",
    response_model=TestConnectionResult,
)
async def test_connection_standalone(
    payload: TestConnectionRequest,
    current_user: User = Depends(get_current_user),  # noqa: ARG001
) -> TestConnectionResult:
    """Probe credentials without persisting anything."""
    return await _probe_connection(
        payload.dialect, payload.connection_meta, payload.credentials
    )


@router.get(
    "/{workspace_id}/connections",
    response_model=list[ConnectionOut],
)
async def list_connections(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ConnectionOut]:
    await _get_owned_workspace(session, workspace_id, current_user)
    rows = await session.execute(
        select(WorkspaceConnection)
        .where(WorkspaceConnection.workspace_id == workspace_id)
        .order_by(WorkspaceConnection.created_at.desc())
    )
    return [
        ConnectionOut(
            id=str(c.id),
            workspace_id=str(c.workspace_id),
            name=c.name,
            dialect=c.dialect,
            status=c.status,
        )
        for c in rows.scalars().all()
    ]


@router.post(
    "/{workspace_id}/connections",
    response_model=ConnectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    workspace_id: UUID,
    payload: ConnectionCreateRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectionOut:
    await _get_owned_workspace(session, workspace_id, current_user)

    # Gate creation on a successful probe — same rule as before, just
    # at the connection level now.
    probe = await _probe_connection(
        payload.dialect, payload.connection_meta, payload.credentials
    )
    if not probe.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": f"connection_{probe.error_kind or 'failed'}",
                "message": probe.error or "Connection test failed",
            },
        )

    conn = WorkspaceConnection(
        workspace_id=workspace_id,
        name=payload.name,
        dialect=payload.dialect,
        connection_meta=payload.connection_meta,
        status="pending",
    )
    session.add(conn)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A connection with that name already exists in this workspace",
        ) from exc

    if payload.credentials and payload.auth_kind != "none":
        blob = json.dumps(payload.credentials, sort_keys=True).encode("utf-8")
        ciphertext, nonce, key_version = crypto.encrypt(blob, aad=str(conn.id).encode())
        session.add(
            WorkspaceCredentials(
                connection_id=conn.id,
                auth_kind=payload.auth_kind,
                ciphertext=ciphertext,
                nonce=nonce,
                key_version=key_version,
            )
        )

    job = ProfileJob(connection_id=conn.id, state="queued")
    session.add(job)
    await session.commit()
    await session.refresh(conn)
    await session.refresh(job)

    _enqueue_profile_job(str(conn.id), str(job.id))

    return ConnectionOut(
        id=str(conn.id),
        workspace_id=str(conn.workspace_id),
        name=conn.name,
        dialect=conn.dialect,
        status=conn.status,
        profile_job_id=str(job.id),
    )


@router.get(
    "/{workspace_id}/connections/{connection_id}",
    response_model=ConnectionOut,
)
async def get_connection(
    workspace_id: UUID,
    connection_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectionOut:
    conn = await _get_owned_connection(
        session, workspace_id, connection_id, current_user
    )
    return ConnectionOut(
        id=str(conn.id),
        workspace_id=str(conn.workspace_id),
        name=conn.name,
        dialect=conn.dialect,
        status=conn.status,
    )


@router.delete(
    "/{workspace_id}/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_connection(
    workspace_id: UUID,
    connection_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    conn = await _get_owned_connection(
        session, workspace_id, connection_id, current_user
    )
    await session.delete(conn)
    await session.commit()


@router.post(
    "/{workspace_id}/connections/{connection_id}/refresh",
    response_model=ConnectionOut,
)
async def refresh_connection(
    workspace_id: UUID,
    connection_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectionOut:
    """Re-run schema profiling for a connection. Enqueues a new job."""
    conn = await _get_owned_connection(
        session, workspace_id, connection_id, current_user
    )
    job = ProfileJob(connection_id=conn.id, state="queued")
    session.add(job)
    conn.status = "profiling"
    await session.commit()
    await session.refresh(job)
    _enqueue_profile_job(str(conn.id), str(job.id))
    return ConnectionOut(
        id=str(conn.id),
        workspace_id=str(conn.workspace_id),
        name=conn.name,
        dialect=conn.dialect,
        status=conn.status,
        profile_job_id=str(job.id),
    )
