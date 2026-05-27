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
from app.db.models import (
    ProfileJob,
    SchemaBundle,
    WorkspaceConnection,
    WorkspaceCredentials,
)
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
def run_profile_job(connection_id: str, profile_job_id: str) -> dict[str, object]:
    """Celery entrypoint. Sync wrapper around the async pipeline.

    Profiles a single WorkspaceConnection. The authoritative job state
    lives in ``profile_jobs``; the result dict is just a quick summary.
    """
    return asyncio.run(_run_async(UUID(connection_id), UUID(profile_job_id)))


async def _run_async(connection_id: UUID, profile_job_id: UUID) -> dict[str, object]:
    engine_sa = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine_sa, expire_on_commit=False)
    try:
        async with Session() as session:
            await _mark_running(session, profile_job_id)
            try:
                conn = await _load_connection(session, connection_id)
                creds_dict = await _load_credentials(session, connection_id)
                # Engines look up creds on a private attr (see PostgresEngine.__init__)
                conn._credentials = creds_dict  # type: ignore[attr-defined]
                qe = get_engine(conn)
                bundle = await profile(qe)
                await qe.aclose()
                await _persist_bundle(session, connection_id, bundle)
                await _mark_succeeded(session, profile_job_id, connection_id)
                # Newly-profiled connection → kick off a RAG reindex
                # so the agent can immediately reason over this schema.
                # Local import avoids a circular import at module load time.
                from app.workers.index_task import run_index_connection
                run_index_connection.delay(str(connection_id))
                return {"ok": True, "tables": len(bundle.tables)}
            except Exception as e:
                log.exception("profile job failed for connection %s", connection_id)
                await _mark_failed(session, profile_job_id, connection_id, str(e))
                return {"ok": False, "error": str(e)}
    finally:
        await engine_sa.dispose()


async def _load_connection(
    session: AsyncSession, connection_id: UUID
) -> WorkspaceConnection:
    result = await session.execute(
        select(WorkspaceConnection).where(WorkspaceConnection.id == connection_id)
    )
    conn = result.scalar_one_or_none()
    if conn is None:
        raise RuntimeError(f"Connection {connection_id} not found")
    return conn


async def _load_credentials(session: AsyncSession, connection_id: UUID) -> dict[str, str]:
    result = await session.execute(
        select(WorkspaceCredentials).where(WorkspaceCredentials.connection_id == connection_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return {}
    # Phase 1 migration moved credentials from workspace_id-keyed to
    # connection_id-keyed PKs but did NOT re-encrypt the payloads. So
    # rows created before that migration still authenticate with
    # ``aad=workspace_id``. Try both AADs; the workspace_id fallback is
    # looked up via the connection FK.
    conn = await session.get(WorkspaceConnection, connection_id)
    aads: list[bytes | None] = [str(connection_id).encode()]
    if conn is not None:
        aads.append(str(conn.workspace_id).encode())
    raw = crypto.decrypt_with_aads(
        row.ciphertext,
        row.nonce,
        key_version=row.key_version,
        aads=aads,
    )
    # Stored shape: JSON {"user": ..., "password": ...} for password auth.
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    # Bare-password legacy shape (auth_kind="dsn" or single-secret).
    return {"password": raw.decode("utf-8", errors="replace")}


async def _persist_bundle(
    session: AsyncSession, connection_id: UUID, bundle
) -> None:
    payload = bundle.model_dump(mode="json")
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()

    existing = await session.execute(
        select(SchemaBundle).where(SchemaBundle.connection_id == connection_id)
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = SchemaBundle(
            connection_id=connection_id,
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
    session: AsyncSession, profile_job_id: UUID, connection_id: UUID
) -> None:
    job = await session.get(ProfileJob, profile_job_id)
    if job is not None:
        job.state = "succeeded"
        job.finished_at = datetime.now(timezone.utc)
    conn = await session.get(WorkspaceConnection, connection_id)
    if conn is not None:
        conn.status = "ready"
    await session.commit()


async def _mark_failed(
    session: AsyncSession, profile_job_id: UUID, connection_id: UUID, err: str
) -> None:
    job = await session.get(ProfileJob, profile_job_id)
    if job is not None:
        job.state = "failed"
        job.error = err
        job.finished_at = datetime.now(timezone.utc)
    conn = await session.get(WorkspaceConnection, connection_id)
    if conn is not None:
        conn.status = "error"
    await session.commit()
