"""Phase 42 — Connection Cluster CRUD endpoints.

A cluster lives inside a workspace and groups N connections that
the user wants to treat as one logical DB (read replicas, shards,
…). The chat scope picker can target a cluster instead of a
single connection; the federation path then fans out across the
cluster's connections.

Routes (all scoped to the calling user's workspaces):

  POST   /workspaces/{wid}/clusters         — create
  GET    /workspaces/{wid}/clusters         — list (with member counts)
  GET    /workspaces/{wid}/clusters/{cid}   — read
  PATCH  /workspaces/{wid}/clusters/{cid}   — rename / re-describe
  DELETE /workspaces/{wid}/clusters/{cid}   — drop (members lose their
                                              cluster_id, are NOT
                                              deleted — see migration)
  POST   /workspaces/{wid}/clusters/{cid}/members  — attach connection
  DELETE /workspaces/{wid}/clusters/{cid}/members/{conn_id}
                                            — detach connection
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import (
    ConnectionCluster,
    User,
    Workspace,
    WorkspaceConnection,
)
from app.db.session import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/workspaces", tags=["clusters"])


# ── Schemas ──────────────────────────────────────────────────────


class CreateClusterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class UpdateClusterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class ClusterOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    member_count: int
    created_at: datetime


class ClusterMemberPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: UUID


# ── helpers ──────────────────────────────────────────────────────


async def _get_owned_workspace(
    session: AsyncSession, workspace_id: UUID, user: User
) -> Workspace:
    ws = await session.get(Workspace, workspace_id)
    if ws is None or ws.owner_id != user.id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Workspace not found"
        )
    return ws


async def _get_owned_cluster(
    session: AsyncSession,
    workspace_id: UUID,
    cluster_id: UUID,
    user: User,
) -> ConnectionCluster:
    await _get_owned_workspace(session, workspace_id, user)
    cluster = await session.get(ConnectionCluster, cluster_id)
    if cluster is None or cluster.workspace_id != workspace_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Cluster not found"
        )
    return cluster


async def _cluster_out(
    session: AsyncSession, cluster: ConnectionCluster
) -> ClusterOut:
    row = (
        await session.execute(
            select(func.count(WorkspaceConnection.id)).where(
                WorkspaceConnection.cluster_id == cluster.id
            )
        )
    ).scalar_one()
    return ClusterOut(
        id=str(cluster.id),
        workspace_id=str(cluster.workspace_id),
        name=cluster.name,
        description=cluster.description,
        member_count=int(row or 0),
        created_at=cluster.created_at,
    )


# ── routes ───────────────────────────────────────────────────────


@router.post(
    "/{workspace_id}/clusters",
    response_model=ClusterOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_cluster(
    workspace_id: UUID,
    payload: CreateClusterRequest,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ClusterOut:
    await _get_owned_workspace(session, workspace_id, user)
    cluster = ConnectionCluster(
        workspace_id=workspace_id,
        name=payload.name,
        description=payload.description,
    )
    session.add(cluster)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A cluster with that name already exists",
        ) from exc
    await session.commit()
    await session.refresh(cluster)
    return await _cluster_out(session, cluster)


@router.get(
    "/{workspace_id}/clusters",
    response_model=list[ClusterOut],
)
async def list_clusters(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ClusterOut]:
    await _get_owned_workspace(session, workspace_id, user)
    rows = (
        await session.execute(
            select(ConnectionCluster)
            .where(ConnectionCluster.workspace_id == workspace_id)
            .order_by(ConnectionCluster.created_at.desc())
        )
    ).scalars().all()
    return [await _cluster_out(session, c) for c in rows]


@router.get(
    "/{workspace_id}/clusters/{cluster_id}",
    response_model=ClusterOut,
)
async def get_cluster(
    workspace_id: UUID,
    cluster_id: UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ClusterOut:
    cluster = await _get_owned_cluster(
        session, workspace_id, cluster_id, user
    )
    return await _cluster_out(session, cluster)


@router.patch(
    "/{workspace_id}/clusters/{cluster_id}",
    response_model=ClusterOut,
)
async def update_cluster(
    workspace_id: UUID,
    cluster_id: UUID,
    payload: UpdateClusterRequest,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ClusterOut:
    cluster = await _get_owned_cluster(
        session, workspace_id, cluster_id, user
    )
    if payload.name is not None:
        cluster.name = payload.name
    if payload.description is not None:
        cluster.description = payload.description
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A cluster with that name already exists",
        ) from exc
    await session.refresh(cluster)
    return await _cluster_out(session, cluster)


@router.delete(
    "/{workspace_id}/clusters/{cluster_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_cluster(
    workspace_id: UUID,
    cluster_id: UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    cluster = await _get_owned_cluster(
        session, workspace_id, cluster_id, user
    )
    # The migration's FK is ON DELETE SET NULL — members keep
    # existing as standalone connections. No need to re-null
    # explicitly here.
    await session.delete(cluster)
    await session.commit()


# ── membership ───────────────────────────────────────────────────


@router.post(
    "/{workspace_id}/clusters/{cluster_id}/members",
    response_model=ClusterOut,
)
async def add_cluster_member(
    workspace_id: UUID,
    cluster_id: UUID,
    payload: ClusterMemberPayload,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ClusterOut:
    """Attach a connection to a cluster. The connection must already
    live in the same workspace — clusters never span workspaces.
    A connection already in another cluster is re-pointed (a
    connection only ever belongs to one cluster)."""
    cluster = await _get_owned_cluster(
        session, workspace_id, cluster_id, user
    )
    conn = await session.get(WorkspaceConnection, payload.connection_id)
    if conn is None or conn.workspace_id != workspace_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Connection not found"
        )
    conn.cluster_id = cluster.id
    await session.commit()
    return await _cluster_out(session, cluster)


@router.delete(
    "/{workspace_id}/clusters/{cluster_id}/members/{connection_id}",
    response_model=ClusterOut,
)
async def remove_cluster_member(
    workspace_id: UUID,
    cluster_id: UUID,
    connection_id: UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ClusterOut:
    cluster = await _get_owned_cluster(
        session, workspace_id, cluster_id, user
    )
    conn = await session.get(WorkspaceConnection, connection_id)
    if conn is None or conn.workspace_id != workspace_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Connection not found"
        )
    if conn.cluster_id == cluster.id:
        conn.cluster_id = None
        await session.commit()
    return await _cluster_out(session, cluster)
