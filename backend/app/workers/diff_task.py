"""Daily schema-drift detector.

Per workspace:

  1. Construct the engine, run :meth:`introspect_schema`.
  2. Compare the fresh bundle to the one stored in ``schema_bundles``.
  3. If :func:`schema_changed` flags anything — sample columns,
     persist the new bundle, then enqueue a full ``index_workspace`` task.

We skip workspaces in ``status != 'ready'`` (they're either still profiling
or in error). The diff intentionally ignores sample values and row counts
so we don't re-embed on routine churn.

Triggered by Celery Beat at the hour configured in :class:`Settings`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import (
    SchemaBundle as SchemaBundleRow,
    Workspace,
    WorkspaceCredentials,
)
from app.engines import register_all as register_engines
from app.engines.base import (
    ColumnMeta,
    ForeignKeyMeta,
    SchemaBundle,
    TableMeta,
)
from app.engines.registry import get_engine
from app.services import crypto
from app.services.rag.differ import schema_changed
from app.services.schema_profiler import profile
from app.workers.celery_app import celery_app
from app.workers.index_task import run_index_workspace

register_engines()
log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.diff_task.run_daily_diff")
def run_daily_diff() -> dict[str, int]:
    """Iterate every workspace, refresh + re-index on structural drift."""
    return asyncio.run(_run_daily_diff_async())


async def _run_daily_diff_async() -> dict[str, int]:
    engine_sa = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine_sa, expire_on_commit=False)
    checked = 0
    changed = 0
    failed = 0
    try:
        async with Session() as session:
            rows = await session.execute(
                select(Workspace).where(Workspace.status == "ready")
            )
            workspaces = list(rows.scalars().all())

        for ws in workspaces:
            try:
                did_change = await _diff_one_workspace(Session, ws.id)
                checked += 1
                if did_change:
                    changed += 1
                    # Enqueue RAG reindex; it'll run on whichever worker
                    # picks it up.
                    run_index_workspace.delay(str(ws.id))
            except Exception:
                log.exception("daily diff failed for workspace %s", ws.id)
                failed += 1
    finally:
        await engine_sa.dispose()

    log.info(
        "rag.daily_diff checked=%d changed=%d failed=%d", checked, changed, failed
    )
    return {"checked": checked, "changed": changed, "failed": failed}


async def _diff_one_workspace(Session, workspace_id: UUID) -> bool:
    async with Session() as session:
        ws = await session.get(Workspace, workspace_id)
        if ws is None:
            return False
        creds = await _load_credentials(session, workspace_id)
        ws._credentials = creds  # type: ignore[attr-defined]
        qe = get_engine(ws)
        try:
            # We do introspect only (no full profiling) for the diff check;
            # samples don't affect the structural fingerprint.
            new_bundle = await qe.introspect_schema()
        finally:
            await qe.aclose()

        old_row = (
            await session.execute(
                select(SchemaBundleRow).where(
                    SchemaBundleRow.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()
        old_bundle = _bundle_from_row(old_row.bundle) if old_row else None

        diff = schema_changed(old_bundle, new_bundle)
        if not diff.changed:
            return False

        log.info(
            "schema drift ws=%s added=%s removed=%s modified=%s",
            workspace_id,
            diff.added_tables,
            diff.removed_tables,
            diff.modified_tables,
        )

        # Re-sample so the bundle is complete, then persist.
        full_bundle = await _resample(ws, new_bundle)
        await _persist_bundle(session, workspace_id, full_bundle)
        return True


async def _resample(ws: Workspace, introspected: SchemaBundle) -> SchemaBundle:
    """Re-run the profiler so the persisted bundle has fresh samples too."""
    qe = get_engine(ws)
    try:
        return await profile(qe)
    finally:
        await qe.aclose()


async def _load_credentials(session, workspace_id: UUID) -> dict[str, str]:
    row = (
        await session.execute(
            select(WorkspaceCredentials).where(
                WorkspaceCredentials.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return {}
    raw = crypto.decrypt(
        row.ciphertext,
        row.nonce,
        key_version=row.key_version,
        aad=str(workspace_id).encode(),
    )
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {"password": raw.decode("utf-8", errors="replace")}


async def _persist_bundle(session, workspace_id: UUID, bundle: SchemaBundle) -> None:
    payload = bundle.model_dump(mode="json")
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    row = (
        await session.execute(
            select(SchemaBundleRow).where(
                SchemaBundleRow.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = SchemaBundleRow(
            workspace_id=workspace_id,
            bundle=payload,
            schema_hash=digest,
            status="ready",
        )
        session.add(row)
    else:
        row.bundle = payload
        row.schema_hash = digest
        row.status = "ready"
        row.refreshed_at = datetime.now(timezone.utc)
    await session.commit()


def _bundle_from_row(raw):
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
    return SchemaBundle(
        dialect=raw["dialect"], tables=tables, samples=raw.get("samples", {}) or {}
    )


__all__ = ["run_daily_diff"]
