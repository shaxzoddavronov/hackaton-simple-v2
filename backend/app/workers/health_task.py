"""Celery task — periodic health probe over every workspace_connection.

Beat fires :func:`run_health_sweep` every 5 minutes; the task iterates
all connections with credentials available (skips ``pending`` rows
that haven't been configured yet), probes each via
:mod:`app.services.connection_health`, and stamps the outcome on the
row. Each connection is independent — one bad probe never blocks the
others, and a connection whose credentials are missing or undecryptable
records ``last_health_ok=False`` with a clear reason instead of
crashing the sweep.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import crypto
from app.services.connection_health import probe_one
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.health_task.run_health_sweep",
    bind=True,
)
def run_health_sweep(self) -> dict[str, int]:
    """Beat-driven sweep. Returns ``{checked, ok, failed, skipped}``
    for observability."""
    return asyncio.run(_sweep_async())


async def _sweep_async() -> dict[str, int]:
    from app.db.models import WorkspaceConnection, WorkspaceCredentials

    eng = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(eng, expire_on_commit=False)

    checked = ok = failed = skipped = 0
    now = datetime.now(timezone.utc)
    try:
        async with Session() as session:
            rows = (
                await session.execute(
                    select(WorkspaceConnection).where(
                        WorkspaceConnection.status.in_(
                            ("ready", "error", "auth_error")
                        )
                    )
                )
            ).scalars().all()
            checked = len(rows)
            for conn in rows:
                creds = await _load_credentials(session, conn.id)
                if creds is None:
                    # Credentials missing / undecryptable — record but
                    # don't probe (would always fail with the same
                    # cryptography error).
                    conn.last_health_check_at = now
                    conn.last_health_ok = False
                    conn.last_health_latency_ms = 0
                    conn.last_health_error = (
                        "credentials unavailable (deleted or "
                        "encrypted with a different QM_MASTER_KEY)"
                    )
                    skipped += 1
                    continue
                result = await probe_one(conn, creds)
                conn.last_health_check_at = now
                conn.last_health_ok = result.ok
                conn.last_health_latency_ms = result.latency_ms
                conn.last_health_error = result.error
                if result.ok:
                    ok += 1
                else:
                    failed += 1
            await session.commit()
    finally:
        await eng.dispose()

    log.info(
        "health_task: sweep checked=%d ok=%d failed=%d skipped=%d",
        checked, ok, failed, skipped,
    )
    return {
        "checked": checked,
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
    }


async def _load_credentials(
    session, connection_id: UUID
) -> dict[str, str] | None:
    """Return the decrypted credentials dict, or ``None`` if the row
    is missing / can't be decrypted under the current master key.

    Mirrors the dual-AAD decrypt path used in diff_task and
    federated_executor so legacy rows (encrypted with workspace_id
    AAD) keep working alongside post-Phase-1 rows (connection_id AAD).
    """
    from app.db.models import WorkspaceConnection, WorkspaceCredentials

    row = (
        await session.execute(
            select(WorkspaceCredentials).where(
                WorkspaceCredentials.connection_id == connection_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    conn = await session.get(WorkspaceConnection, connection_id)
    aads: list[bytes | None] = [str(connection_id).encode()]
    if conn is not None:
        aads.append(str(conn.workspace_id).encode())
    try:
        raw = crypto.decrypt_with_aads(
            row.ciphertext,
            row.nonce,
            key_version=row.key_version,
            aads=aads,
        )
    except Exception:
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {"password": raw.decode("utf-8", errors="replace")}


__all__ = ["run_health_sweep"]
