from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import ProfileJob, SchemaBundle, Workspace, WorkspaceCredentials
from app.engines import register_all as register_engines
from app.engines.registry import get_engine
from app.services import crypto
from app.services.schema_profiler import profile
from app.workers.celery_app import celery_app

# Register concrete engine adapters on worker import so the task body can
# call `get_engine(workspace)` straight away.
register_engines()

log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.profile_task.run_profile_job")
def run_profile_job(workspace_id: str, profile_job_id: str) -> dict[str, object]:
    """Celery entrypoint. Sync wrapper around the async pipeline.

    Returns a small status dict so the result backend has something
    inspectable — the authoritative record lives in `profile_jobs`.
    """
    return asyncio.run(_run_async(UUID(workspace_id), UUID(profile_job_id)))


async def _run_async(workspace_id: UUID, profile_job_id: UUID) -> dict[str, object]:
    engine_sa = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine_sa, expire_on_commit=False)
    try:
        async with Session() as session:
            await _mark_running(session, profile_job_id)
            try:
                workspace = await _load_workspace(session, workspace_id)
                creds_dict = await _load_credentials(session, workspace_id)
                # Engines look up creds on a private attr (see PostgresEngine.__init__)
                workspace._credentials = creds_dict  # type: ignore[attr-defined]
                qe = get_engine(workspace)
                bundle = await profile(qe)
                await qe.aclose()
                await _persist_bundle(session, workspace_id, bundle)
                await _mark_succeeded(session, profile_job_id, workspace_id)
                # Newly-profiled workspace → kick off a full RAG reindex
                # so the agent can immediately reason over this schema.
                # Local import avoids a circular import at module load time
                # (index_task imports celery_app, which imports us).
                from app.workers.index_task import run_index_workspace
                run_index_workspace.delay(str(workspace_id))
                return {"ok": True, "tables": len(bundle.tables)}
            except Exception as e:
                log.exception("profile job failed for workspace %s", workspace_id)
                await _mark_failed(session, profile_job_id, workspace_id, str(e))
                return {"ok": False, "error": str(e)}
    finally:
        await engine_sa.dispose()


async def _load_workspace(session: AsyncSession, workspace_id: UUID) -> Workspace:
    result = await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise RuntimeError(f"Workspace {workspace_id} not found")
    return ws


async def _load_credentials(session: AsyncSession, workspace_id: UUID) -> dict[str, str]:
    result = await session.execute(
        select(WorkspaceCredentials).where(WorkspaceCredentials.workspace_id == workspace_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return {}
    raw = crypto.decrypt(
        row.ciphertext,
        row.nonce,
        key_version=row.key_version,
        aad=str(workspace_id).encode(),
    )
    # Stored shape: JSON {"user": ..., "password": ...} for password auth.
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    # Bare-password legacy shape (auth_kind="dsn" or single-secret): expose as "password".
    return {"password": raw.decode("utf-8", errors="replace")}


async def _persist_bundle(session: AsyncSession, workspace_id: UUID, bundle) -> None:
    payload = bundle.model_dump(mode="json")
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()

    existing = await session.execute(
        select(SchemaBundle).where(SchemaBundle.workspace_id == workspace_id)
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = SchemaBundle(
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


async def _mark_running(session: AsyncSession, profile_job_id: UUID) -> None:
    job = await session.get(ProfileJob, profile_job_id)
    if job is None:
        return
    job.state = "running"
    job.started_at = datetime.now(timezone.utc)
    await session.commit()


async def _mark_succeeded(
    session: AsyncSession, profile_job_id: UUID, workspace_id: UUID
) -> None:
    job = await session.get(ProfileJob, profile_job_id)
    if job is not None:
        job.state = "succeeded"
        job.finished_at = datetime.now(timezone.utc)
    ws = await session.get(Workspace, workspace_id)
    if ws is not None:
        ws.status = "ready"
    await session.commit()


async def _mark_failed(
    session: AsyncSession, profile_job_id: UUID, workspace_id: UUID, err: str
) -> None:
    job = await session.get(ProfileJob, profile_job_id)
    if job is not None:
        job.state = "failed"
        job.error = err
        job.finished_at = datetime.now(timezone.utc)
    ws = await session.get(Workspace, workspace_id)
    if ws is not None:
        ws.status = "error"
    await session.commit()
