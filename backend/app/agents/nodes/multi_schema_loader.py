"""Schema loader for the federated path.

For ``intent == "federated_query"`` the agent needs to see EVERY
ready connection in the workspace at once so the planner can write
sub-queries against the right DB for each piece of the question. This
node pulls all bundles in one DB round-trip and writes them to
``state.connection_bundles`` keyed by connection-id-as-string.

Workspaces with no ready connections route to ``error_responder`` via
``error_message``; workspaces with only one ready connection still go
through this path (federated_planner can emit a single-sub_query plan
with no merges).
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.state import GraphState
from app.config import settings
from app.db.models import SchemaBundle as SchemaBundleRow, WorkspaceConnection
from app.engines.base import (
    ColumnMeta,
    ForeignKeyMeta,
    SchemaBundle,
    TableMeta,
)

log = logging.getLogger(__name__)


def _deserialize(raw: Any) -> SchemaBundle:
    if isinstance(raw, str):
        raw = json.loads(raw)
    tables: list[TableMeta] = []
    for t in raw.get("tables", []):
        cols = [ColumnMeta(**c) for c in t.get("columns", [])]
        fks = [ForeignKeyMeta(**fk) for fk in t.get("foreign_keys", [])]
        tables.append(
            TableMeta(
                schema=t.get("schema", "public"),
                name=t["name"],
                columns=cols,
                foreign_keys=fks,
                row_count_estimate=t.get("row_count_estimate"),
            )
        )
    return SchemaBundle(
        dialect=raw["dialect"], tables=tables, samples=raw.get("samples", {}) or {}
    )


async def run(state: GraphState) -> GraphState:
    workspace_id = state.get("resolved_workspace_id")
    if workspace_id is None:
        return {
            "intent": "clarify",
            "error_message": "no workspace resolved for federation",
        }

    sa_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    try:
        async with Session() as session:
            # Phase 42 — when the chat API resolved a scope wider
            # than `database`, it populated `scope_connection_ids`.
            # Honour that filter so the federation runs across only
            # the cluster / scope the user picked, not every
            # workspace connection. Empty / unset = legacy behaviour
            # (every ready connection).
            scope_ids = state.get("scope_connection_ids") or []
            stmt = (
                select(WorkspaceConnection)
                .where(
                    WorkspaceConnection.workspace_id == workspace_id,
                    WorkspaceConnection.status == "ready",
                )
                .order_by(WorkspaceConnection.created_at)
            )
            if scope_ids:
                stmt = stmt.where(WorkspaceConnection.id.in_(scope_ids))
            conn_rows = await session.execute(stmt)
            connections = list(conn_rows.scalars().all())
            if not connections:
                return {
                    "error_message": (
                        f"Workspace {workspace_id} has no ready connections; "
                        "add at least one database before federated queries."
                    )
                }
            bundles: dict[str, SchemaBundle] = {}
            for c in connections:
                row = (
                    await session.execute(
                        select(SchemaBundleRow).where(
                            SchemaBundleRow.connection_id == c.id
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    log.warning(
                        "multi_schema_loader: connection %s has no bundle yet",
                        c.id,
                    )
                    continue
                bundles[str(c.id)] = _deserialize(row.bundle)
    finally:
        await sa_engine.dispose()

    if not bundles:
        return {
            "error_message": (
                "No connections have a profiled schema yet — wait for "
                "the profile jobs to finish or refresh manually."
            )
        }

    # Also surface the first bundle on the single-connection slot so any
    # downstream node that still reads `schema_bundle` (rag_retriever,
    # chart hints) has something to work with.
    first_bundle = next(iter(bundles.values()))
    return {
        "schema_bundle": first_bundle,
        "connection_bundles": bundles,
    }
