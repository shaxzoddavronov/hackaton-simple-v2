"""Phase 35 — periodic health probes for workspace connections.

The Celery-beat task in :mod:`app.workers.health_task` iterates every
enabled connection every few minutes and calls :func:`probe_one`
below. The probe never returns a payload — it just decides
"reachable / not reachable" and stamps the outcome onto the
connection row so the UI can render a status dot without paying the
probe latency on every page load.

Probe shape per dialect:

  * SQL families (postgres / sqlite / mysql / clickhouse / oracle /
    duckdb / mssql / snowflake / bigquery) → ``SELECT 1`` via the
    engine's own ``execute`` path. That exercises the read-only
    runtime guard + driver + auth in one shot.
  * Elasticsearch → ``GET /_cluster/health`` (cheap, no scrolling).
  * MongoDB → ``db.command({ping: 1})``.
  * REST API → ``GET /v1`` or whatever the engine reports as base.
  * GraphQL → ``{ __typename }`` introspection-free no-op query.

Failure isolation: one bad connection NEVER blocks the sweep. Every
exception caught here turns into ``ok=False`` plus a sanitised error
string on the row.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.engines.base import Dialect

log = logging.getLogger(__name__)

# Per-probe ceiling. Long-running queries shouldn't block the sweep.
PROBE_TIMEOUT_S = 8


@dataclass(slots=True)
class HealthResult:
    ok: bool
    latency_ms: int
    error: str | None = None


def _sanitize_error(exc: Exception) -> str:
    """Strip filesystem paths / IPs / ports / passwords from the
    error message so the value stamped on the connection row is
    safe to surface in the UI."""
    s = str(exc)
    # Drop tracebacks past the first newline.
    s = s.splitlines()[0] if s else ""
    # Trim absurdly long messages; UI tooltips can't render multi-KB.
    return s[:240] or exc.__class__.__name__


async def probe_one(conn_row: Any, creds: dict[str, str]) -> HealthResult:
    """Run a dialect-appropriate liveness probe and return the
    timing outcome. ``conn_row`` is a ``WorkspaceConnection`` ORM
    instance; ``creds`` is the decrypted credentials dict (same shape
    the engine constructor receives via ``source._credentials``).
    """
    dialect: Dialect = conn_row.dialect  # type: ignore[assignment]
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(
            _dispatch(dialect, conn_row, creds),
            timeout=PROBE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return HealthResult(
            ok=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            error=f"probe exceeded {PROBE_TIMEOUT_S}s",
        )
    except Exception as e:  # noqa: BLE001 — probe boundary, catch all
        return HealthResult(
            ok=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            error=_sanitize_error(e),
        )
    return HealthResult(
        ok=True,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )


async def _dispatch(
    dialect: Dialect,
    conn_row: Any,
    creds: dict[str, str],
) -> None:
    """Per-dialect probe. Returns nothing on success, raises on
    failure. Keep cheap — this runs every few minutes per
    connection."""
    # Stash creds on the duck-typed source the engine constructor
    # expects (matches the pattern used in federated_executor and
    # diff_task).
    setattr(conn_row, "_credentials", creds)

    from app.engines import register_all
    from app.engines.registry import get_engine

    register_all()

    if dialect == "elasticsearch":
        await _probe_elasticsearch(conn_row, creds)
        return
    if dialect == "mongodb":
        await _probe_mongodb(conn_row, creds)
        return
    if dialect == "graphql":
        await _probe_graphql(conn_row, creds)
        return
    if dialect == "rest_api":
        await _probe_rest_api(conn_row, creds)
        return

    # SQL family — use the engine's own execute() with SELECT 1.
    engine = get_engine(conn_row)
    try:
        await engine.execute(
            "SELECT 1 AS ok", row_cap=1, timeout_s=PROBE_TIMEOUT_S
        )
    finally:
        await engine.aclose()


async def _probe_elasticsearch(
    conn_row: Any, creds: dict[str, str]
) -> None:
    """ES has a dedicated cluster-health endpoint that's cheaper than
    a SEARCH on _cluster_health."""
    from app.engines import register_all
    from app.engines.registry import get_engine

    register_all()
    engine = get_engine(conn_row)
    try:
        # The ES engine wraps an async client; use a tiny match_all
        # search on _cluster which always exists.
        client = getattr(engine, "_client", None)
        if client is None:
            raise RuntimeError("Elasticsearch engine has no _client attr")
        await client.cluster.health()
    finally:
        await engine.aclose()


async def _probe_mongodb(conn_row: Any, creds: dict[str, str]) -> None:
    from app.engines import register_all
    from app.engines.registry import get_engine

    register_all()
    engine = get_engine(conn_row)
    try:
        client = getattr(engine, "_client", None)
        if client is None:
            raise RuntimeError("MongoDB engine has no _client attr")
        # `ping` is the canonical admin liveness command.
        db_name = (conn_row.connection_meta or {}).get("db_name") or "admin"
        await client[db_name].command("ping")
    finally:
        await engine.aclose()


async def _probe_graphql(conn_row: Any, creds: dict[str, str]) -> None:
    """`{ __typename }` is the smallest valid GraphQL query."""
    import json

    from app.engines import register_all
    from app.engines.registry import get_engine

    register_all()
    engine = get_engine(conn_row)
    try:
        envelope = json.dumps({"query": "{ __typename }"})
        await engine.execute(
            envelope, row_cap=1, timeout_s=PROBE_TIMEOUT_S
        )
    finally:
        await engine.aclose()


async def _probe_rest_api(conn_row: Any, creds: dict[str, str]) -> None:
    """Hit the base_url with a HEAD; many APIs reject HEAD with 405
    but that's still a "reachable" signal. Fall back to GET on 405."""
    import httpx

    meta = conn_row.connection_meta or {}
    base = str(meta.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("rest_api connection_meta missing base_url")
    timeout = httpx.Timeout(PROBE_TIMEOUT_S)
    headers: dict[str, str] = dict(meta.get("default_headers") or {})
    auth_kind = getattr(conn_row, "auth_kind", None) or "none"
    if auth_kind == "bearer" and creds.get("token"):
        headers["Authorization"] = f"Bearer {creds['token']}"
    elif auth_kind == "api_key" and creds.get("key"):
        loc = creds.get("key_location") or "header"
        name = creds.get("key_name") or "X-API-Key"
        if loc == "header":
            headers[name] = creds["key"]
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.head(base, headers=headers)
        except httpx.HTTPError:
            r = await client.get(base, headers=headers)
        if r.status_code == 405:
            r = await client.get(base, headers=headers)
        if r.status_code >= 500:
            raise RuntimeError(
                f"REST endpoint returned HTTP {r.status_code}"
            )


__all__ = ["HealthResult", "PROBE_TIMEOUT_S", "probe_one"]
