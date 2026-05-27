from __future__ import annotations

import json
from typing import Any

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
from app.services.schema_pruner import prune


async def run(state: GraphState) -> GraphState:
    # Connection is the source of truth post-Phase-1. If the caller
    # only resolved a workspace (e.g., older client), we auto-pick the
    # first 'ready' connection inside it.
    connection_id = state.get("resolved_connection_id")
    workspace_id = state.get("resolved_workspace_id")
    if connection_id is None and workspace_id is None:
        return {
            "intent": "clarify",
            "error_message": "no workspace or connection resolved",
        }

    sa_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    try:
        async with Session() as session:
            if connection_id is None:
                # Fall back to the first ready connection in the workspace.
                pick = await session.execute(
                    select(WorkspaceConnection)
                    .where(
                        WorkspaceConnection.workspace_id == workspace_id,
                        WorkspaceConnection.status == "ready",
                    )
                    .order_by(WorkspaceConnection.created_at)
                    .limit(1)
                )
                conn_row = pick.scalar_one_or_none()
                if conn_row is None:
                    return {
                        "error_message": (
                            f"Workspace {workspace_id} has no ready connection yet"
                        )
                    }
                connection_id = conn_row.id
            row = await session.execute(
                select(SchemaBundleRow).where(
                    SchemaBundleRow.connection_id == connection_id
                )
            )
            bundle_row = row.scalar_one_or_none()
    finally:
        await sa_engine.dispose()

    if bundle_row is None:
        return {
            "error_message": (
                f"Connection {connection_id} has no profiled schema yet"
            ),
        }

    bundle = _deserialize(bundle_row.bundle)
    out: GraphState = {
        "schema_bundle": bundle,
        "resolved_connection_id": connection_id,
    }

    # Skip pruning for metadata intent (the answer writer wants the whole bundle).
    if state.get("intent") in {"data_query", "dashboard"}:
        pruned = prune(bundle, state.get("user_message", ""))
        out["pruned_table_qnames"] = pruned.selected_tables
    return out


def _deserialize(raw: Any) -> SchemaBundle:
    if isinstance(raw, str):
        raw = json.loads(raw)
    tables = []
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
    samples = raw.get("samples", {}) or {}
    return SchemaBundle(dialect=raw["dialect"], tables=tables, samples=samples)
