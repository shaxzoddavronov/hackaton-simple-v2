"""Redis-backed cache for query-executor results.

Repeated questions hit the same SQL/ES/Mongo against the same
connection — re-running them wastes both DB time and the agent's
latency budget. Phase 23 caches each ``(connection_id, sql)`` pair in
the existing Redis instance (the same one Celery + slowapi use) so
the second-and-subsequent calls return instantly from memory.

Cache hit semantics:

  * Key: ``qm:qcache:{connection_id}:sha256(sql_normalised)``.
    Connection scope means re-profiling or deleting a connection
    naturally invalidates its rows (we wipe the prefix in
    :func:`invalidate_connection`).
  * Value: JSON-encoded :class:`ResultSet` (small enough to fit —
    we cap at ``QUERY_CACHE_MAX_BYTES`` before storing). ``ResultSet``
    serialises cleanly via Pydantic ``model_dump_json``.
  * TTL: ``QUERY_CACHE_TTL_S`` (default 300 s — 5 minutes). Picked
    short by default because a chat workspace's DBs are usually
    live OLTP; raise to hours for analytics warehouses where the
    same dashboard repeats all day.

What we DO NOT cache:

  * Plans that returned a row_count above ``QUERY_CACHE_MAX_ROWS``
    (default 5000) — the payload would dominate Redis. Charts pull
    aggregates anyway; the 5000-row safety net catches accidental
    "show me everything" queries.
  * Federated plans (multiple sub-queries). Their results are
    composed in Python — caching the merged ResultSet would skip
    the per-sub-query staleness signal. v2 work.
  * Anything where the SQL is empty / non-string.

The cache is purely an optimisation; every call returns through
the same engine.execute path on miss, and a Redis outage degrades
gracefully (we log, return None, and the executor runs the query).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.engines.base import ResultSet

log = logging.getLogger(__name__)


_CACHE_PREFIX = "qm:qcache"


def _normalise_sql(sql: str) -> str:
    """Strip leading/trailing whitespace and collapse internal runs.

    Identical-modulo-whitespace queries map to the same cache key.
    Comments are NOT stripped because they're rare in agent-emitted
    SQL (we ban them in the planner prompt) and stripping would
    require a full SQL parser we don't want to depend on here.
    """
    return " ".join((sql or "").split())


def _key(connection_id: str, sql: str) -> str:
    h = hashlib.sha256(_normalise_sql(sql).encode("utf-8")).hexdigest()
    return f"{_CACHE_PREFIX}:{connection_id}:{h}"


def _redis_client():
    """Return a sync redis client or ``None`` when Redis is
    unreachable / disabled.

    Lazy import so unit-test modules that monkeypatch don't pay
    the redis dependency cost at collection time.
    """
    if not settings.QUERY_CACHE_ENABLED:
        return None
    try:
        import redis

        return redis.Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=1.0,
            socket_timeout=1.5,
            decode_responses=False,
        )
    except Exception as e:
        log.warning("query_cache: redis init failed: %s", e)
        return None


async def get_cached(
    connection_id: str, sql: str
) -> "ResultSet | None":
    """Return a cached ResultSet for ``(connection_id, sql)`` or
    None on miss / Redis outage.

    Async signature so the caller (executor node) doesn't change
    shape; the underlying redis client is sync but a GET is fast
    enough that we don't bother threading it.
    """
    client = _redis_client()
    if client is None:
        return None
    key = _key(connection_id, sql)
    try:
        raw = client.get(key)
    except Exception as e:
        log.warning("query_cache: GET failed: %s", e)
        return None
    if raw is None:
        return None
    try:
        # Local import to avoid the top-level circular reference
        # (engines.base depends on app.services indirectly via tests).
        from app.engines.base import ResultSet

        return ResultSet.model_validate_json(raw)
    except Exception as e:
        log.warning(
            "query_cache: stale entry — failed to deserialise: %s", e
        )
        return None


async def set_cached(
    connection_id: str, sql: str, rs: "ResultSet"
) -> bool:
    """Store ``rs`` under ``(connection_id, sql)``. Returns True
    when stored, False when skipped (cache disabled / Redis down /
    payload too large / too many rows).
    """
    client = _redis_client()
    if client is None:
        return False
    if rs.row_count > settings.QUERY_CACHE_MAX_ROWS:
        return False
    payload = rs.model_dump_json()
    if len(payload) > settings.QUERY_CACHE_MAX_BYTES:
        return False
    key = _key(connection_id, sql)
    try:
        client.set(key, payload, ex=settings.QUERY_CACHE_TTL_S)
        return True
    except Exception as e:
        log.warning("query_cache: SET failed: %s", e)
        return False


async def invalidate_connection(connection_id: str) -> int:
    """Drop every cached entry tied to ``connection_id``. Called
    from the re-profile / delete-connection paths so a schema
    change can never serve up a stale row shape.

    Returns the number of keys removed.
    """
    client = _redis_client()
    if client is None:
        return 0
    pattern = f"{_CACHE_PREFIX}:{connection_id}:*"
    try:
        # SCAN over the prefix so we don't block Redis on a large
        # keyspace (KEYS would).
        removed = 0
        for key in client.scan_iter(match=pattern, count=200):
            try:
                client.delete(key)
                removed += 1
            except Exception:
                continue
        return removed
    except Exception as e:
        log.warning("query_cache: invalidate failed: %s", e)
        return 0


__all__ = [
    "get_cached",
    "set_cached",
    "invalidate_connection",
]
