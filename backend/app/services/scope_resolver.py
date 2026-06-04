"""Phase 42 — translate the chat ``scope`` enum into a concrete
set of connection ids for the agent to fan out across.

The chat API picks a ``ChatScope`` and (optionally) a
``scope_cluster_id`` / ``scope_table``. The federation path
expects an explicit list of connection ids. This module is the
adapter — pure Python + a single async DB read.

Empty result is meaningful: if a scope yields zero connections
(e.g. the workspace has no clusters but the user asked for
``cluster``), the caller surfaces a clarify response. We never
silently fall back to "all connections".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkspaceConnection

log = logging.getLogger(__name__)


Scope = Literal[
    "table",
    "database",
    "all_databases",
    "cluster",
    "all_clusters",
    "all_connections",
]


@dataclass(slots=True)
class ResolvedScope:
    """Resolved view of a chat scope.

    ``connection_ids`` is empty when the scope can't be resolved
    (no such cluster, no clusters at all, no active connection).
    The caller decides how to surface — usually as a clarify
    response with quick-reply chips.
    """
    scope: Scope
    connection_ids: list[UUID]
    federation: bool
    error: str | None = None


async def resolve_scope(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    scope: Scope,
    active_connection_id: UUID | None,
    scope_cluster_id: UUID | None,
) -> ResolvedScope:
    """Translate the requested scope into a list of connection ids.

    Rules:
      * ``table`` / ``database``: needs an active_connection_id.
        Treated identically here — the table narrowing happens at
        schema_loader, the connection set is the same single id.
      * ``cluster``: needs scope_cluster_id; returns every
        connection whose cluster_id matches.
      * ``all_databases`` / ``all_connections``: every ready
        connection in the workspace.
      * ``all_clusters``: every connection whose cluster_id IS NOT
        NULL.

    ``federation=True`` whenever the scope produces > 1 connection
    OR is one of the multi-connection scope keywords (so the
    coordinator routes to multi_schema_loader → federated_planner
    even when there happens to be only one match).
    """
    if scope in ("table", "database"):
        if active_connection_id is None:
            return ResolvedScope(
                scope=scope,
                connection_ids=[],
                federation=False,
                error=(
                    "Pick a connection (or wider scope) first — "
                    f"scope={scope!r} needs one."
                ),
            )
        return ResolvedScope(
            scope=scope,
            connection_ids=[active_connection_id],
            federation=False,
        )

    if scope == "cluster":
        if scope_cluster_id is None:
            return ResolvedScope(
                scope=scope,
                connection_ids=[],
                federation=True,
                error="scope=cluster requires scope_cluster_id",
            )
        rows = await session.execute(
            select(WorkspaceConnection.id).where(
                WorkspaceConnection.workspace_id == workspace_id,
                WorkspaceConnection.cluster_id == scope_cluster_id,
                WorkspaceConnection.status == "ready",
            )
        )
        ids = [_as_uuid(r[0]) for r in rows.all()]
        if not ids:
            return ResolvedScope(
                scope=scope,
                connection_ids=[],
                federation=True,
                error="cluster has no ready connections",
            )
        return ResolvedScope(
            scope=scope, connection_ids=ids, federation=True
        )

    if scope == "all_clusters":
        rows = await session.execute(
            select(WorkspaceConnection.id).where(
                WorkspaceConnection.workspace_id == workspace_id,
                WorkspaceConnection.cluster_id.is_not(None),
                WorkspaceConnection.status == "ready",
            )
        )
        ids = [_as_uuid(r[0]) for r in rows.all()]
        if not ids:
            return ResolvedScope(
                scope=scope,
                connection_ids=[],
                federation=True,
                error="no clustered connections in this workspace",
            )
        return ResolvedScope(
            scope=scope, connection_ids=ids, federation=True
        )

    # all_databases + all_connections — every ready connection
    rows = await session.execute(
        select(WorkspaceConnection.id).where(
            WorkspaceConnection.workspace_id == workspace_id,
            WorkspaceConnection.status == "ready",
        )
    )
    ids = [_as_uuid(r[0]) for r in rows.all()]
    if not ids:
        return ResolvedScope(
            scope=scope,
            connection_ids=[],
            federation=True,
            error="no ready connections in this workspace",
        )
    return ResolvedScope(
        scope=scope, connection_ids=ids, federation=True
    )


def _as_uuid(value: object) -> UUID:
    """Coerce whatever the column returned into a UUID. Postgres
    surfaces UUID objects natively (UUIDType uses
    ``PG_UUID(as_uuid=True)``); SQLite tests use plain TEXT and
    return ``str``. Both shapes are normalised here so federation
    downstream code can always compare via ``==``."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


__all__ = ["ResolvedScope", "Scope", "resolve_scope"]
