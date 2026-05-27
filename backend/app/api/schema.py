"""Schema endpoint — now scoped per-connection.

Each WorkspaceConnection has its own profiled SchemaBundle. The route
takes both the workspace and the connection ids so the URL itself
documents the ownership chain.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import (
    SchemaBundle as SchemaBundleRow,
    User,
    Workspace,
    WorkspaceConnection,
)
from app.db.session import get_db

router = APIRouter(prefix="/workspaces", tags=["schema"])


@router.get("/{workspace_id}/connections/{connection_id}/schema")
async def get_connection_schema(
    workspace_id: UUID,
    connection_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ws = await session.get(Workspace, workspace_id)
    if ws is None or ws.owner_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    conn = await session.get(WorkspaceConnection, connection_id)
    if conn is None or conn.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")

    row = await session.execute(
        select(SchemaBundleRow).where(SchemaBundleRow.connection_id == connection_id)
    )
    bundle_row = row.scalar_one_or_none()
    if bundle_row is None:
        return {
            "workspace_id": str(workspace_id),
            "connection_id": str(connection_id),
            "status": conn.status,
            "bundle": None,
            "message": "Schema not profiled yet. Wait for the profile job to finish.",
        }

    return {
        "workspace_id": str(workspace_id),
        "connection_id": str(connection_id),
        "status": bundle_row.status,
        "refreshed_at": bundle_row.refreshed_at.isoformat(),
        "schema_hash": bundle_row.schema_hash,
        "bundle": bundle_row.bundle,
    }


@router.get("/{workspace_id}/schema")
async def get_workspace_schema_summary(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Aggregate view: one workspace, all its connections + bundle status."""
    ws = await session.get(Workspace, workspace_id)
    if ws is None or ws.owner_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    conns = await session.execute(
        select(WorkspaceConnection)
        .where(WorkspaceConnection.workspace_id == workspace_id)
        .order_by(WorkspaceConnection.created_at)
    )
    items: list[dict[str, Any]] = []
    for c in conns.scalars().all():
        bundle_row = (
            await session.execute(
                select(SchemaBundleRow).where(
                    SchemaBundleRow.connection_id == c.id
                )
            )
        ).scalar_one_or_none()
        items.append(
            {
                "connection_id": str(c.id),
                "name": c.name,
                "dialect": c.dialect,
                "status": c.status,
                "bundle_ready": bundle_row is not None,
                "table_count": (
                    len(bundle_row.bundle.get("tables", []))
                    if bundle_row and isinstance(bundle_row.bundle, dict)
                    else None
                ),
            }
        )
    return {
        "workspace_id": str(workspace_id),
        "name": ws.name,
        "connections": items,
    }
