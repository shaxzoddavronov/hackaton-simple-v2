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
from app.db.models import ProfileJob, User, Workspace, WorkspaceCredentials
from app.db.session import get_db
from app.engines.registry import get_engine
from app.services import crypto

log = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

# How long we let a connection probe run before declaring it dead.
# Long enough for a slow remote DB on first connect; short enough
# that a broken host doesn't tie up the request thread.
_CONNECTION_TEST_TIMEOUT_S = 8.0


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    dialect: Literal["postgres", "sqlite"]
    connection_meta: dict[str, Any] = Field(default_factory=dict)
    auth_kind: Literal["password", "dsn", "iam", "none"] = "password"
    credentials: dict[str, str] = Field(default_factory=dict)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    dialect: str
    status: str
    profile_job_id: str | None = None


class TestConnectionRequest(BaseModel):
    """Same shape as :class:`CreateWorkspaceRequest` minus the ``name``.

    Lets the frontend probe credentials before committing to a workspace
    row. The handler never writes anything to the DB.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: Literal["postgres", "sqlite"]
    connection_meta: dict[str, Any] = Field(default_factory=dict)
    auth_kind: Literal["password", "dsn", "iam", "none"] = "password"
    credentials: dict[str, str] = Field(default_factory=dict)


class TestConnectionResult(BaseModel):
    ok: bool
    # On success — what we found. Helps the user confirm they pointed
    # us at the right database before clicking Create.
    dialect: str | None = None
    table_count: int | None = None
    table_names_preview: list[str] | None = None
    # On failure — what went wrong. Plain string is easier for the UI
    # to surface than a structured error tree.
    error: str | None = None
    error_kind: str | None = None  # "auth" | "network" | "timeout" | "config" | "other"


async def _probe_connection(
    dialect: str,
    connection_meta: dict[str, Any],
    credentials: dict[str, str],
) -> TestConnectionResult:
    """Construct a transient engine and try to introspect its schema.

    A successful introspection proves: the host is reachable, the
    credentials authenticate, the database name is valid, and the
    schema endpoint responds within the budget. That's the strongest
    smoke check we can run without persisting anything.
    """
    fake_ws = SimpleNamespace(
        dialect=dialect,
        connection_meta=connection_meta,
        _credentials=credentials,
    )

    qe = None
    try:
        qe = get_engine(fake_ws)
    except ValueError as e:
        # Engine __init__ raises this when required keys are missing.
        return TestConnectionResult(
            ok=False, error=str(e), error_kind="config"
        )
    except Exception as e:  # pragma: no cover - defensive
        return TestConnectionResult(
            ok=False, error=str(e), error_kind="other"
        )

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
                "connection refused",
                "could not connect",
                "host",
                "name or service",
                "unreachable",
                "getaddrinfo",
                "dns",
                "no such host",
                "network is unreachable",
            )
        ):
            kind = "network"
        return TestConnectionResult(ok=False, error=msg, error_kind=kind)
    finally:
        if qe is not None:
            try:
                await qe.aclose()
            except Exception:  # pragma: no cover - close is best-effort
                pass

    qnames = [f"{t.schema}.{t.name}" for t in bundle.tables]
    return TestConnectionResult(
        ok=True,
        dialect=bundle.dialect,
        table_count=len(qnames),
        table_names_preview=qnames[:10],
    )


@router.post(
    "/test-connection",
    response_model=TestConnectionResult,
)
async def test_connection(
    payload: TestConnectionRequest,
    current_user: User = Depends(get_current_user),  # noqa: ARG001 — gated by auth
) -> TestConnectionResult:
    """Dry-run probe — does NOT persist a workspace row.

    Returns 200 with ``ok=false`` on failure rather than 4xx so the UI
    can show the structured error without parsing exception bodies.
    """
    return await _probe_connection(
        payload.dialect, payload.connection_meta, payload.credentials
    )


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
    # Gate creation on a successful connection probe. We don't want a
    # workspace row whose status is permanently 'error' or 'auth_error'
    # cluttering the user's list — better to refuse up front with a
    # clear message.
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

    ws = Workspace(
        owner_id=current_user.id,
        name=payload.name,
        dialect=payload.dialect,
        connection_meta=payload.connection_meta,
        status="pending",
    )
    session.add(ws)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A workspace with that name already exists",
        ) from exc

    if payload.credentials and payload.auth_kind != "none":
        blob = json.dumps(payload.credentials, sort_keys=True).encode("utf-8")
        ciphertext, nonce, key_version = crypto.encrypt(blob, aad=str(ws.id).encode())
        creds_row = WorkspaceCredentials(
            workspace_id=ws.id,
            auth_kind=payload.auth_kind,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
        )
        session.add(creds_row)

    job = ProfileJob(workspace_id=ws.id, state="queued")
    session.add(job)
    await session.commit()
    await session.refresh(ws)
    await session.refresh(job)

    _enqueue_profile_job(str(ws.id), str(job.id))

    return WorkspaceOut(
        id=str(ws.id),
        name=ws.name,
        dialect=ws.dialect,
        status=ws.status,
        profile_job_id=str(job.id),
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
    return [
        WorkspaceOut(
            id=str(w.id),
            name=w.name,
            dialect=w.dialect,
            status=w.status,
        )
        for w in rows.scalars().all()
    ]


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceOut:
    ws = await session.get(Workspace, workspace_id)
    if ws is None or ws.owner_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return WorkspaceOut(id=str(ws.id), name=ws.name, dialect=ws.dialect, status=ws.status)


def _enqueue_profile_job(workspace_id: str, profile_job_id: str) -> None:
    """Enqueue the Celery profile task. Isolated so tests can monkeypatch."""
    from app.workers.profile_task import run_profile_job

    run_profile_job.delay(workspace_id, profile_job_id)
