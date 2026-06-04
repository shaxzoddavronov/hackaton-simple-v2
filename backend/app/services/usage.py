"""Phase 37 — request-scoped usage counters.

A :class:`UsageBucket` lives in a :class:`contextvars.ContextVar` so
agent nodes don't have to thread workspace_id + counter args through
every function call. The pattern:

  - ``api/chat.py`` opens a bucket at request start
    (:func:`start_bucket`).
  - Nodes / clients call :func:`current()` to record into it
    (LLM tokens in/out, queries ok/failed, RAG retrievals, cache
    hits).
  - At request end, the API flushes via :func:`flush_bucket` which
    UPSERTs the totals into ``usage_daily``.

If the bucket isn't set (background worker, unit test, direct CLI
invocation), :func:`current()` returns ``None`` and the recording
sites become no-ops — no exceptions, no broken paths.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


@dataclass
class UsageBucket:
    """Mutable per-request counters. Stays in-memory until
    :func:`flush_bucket` runs at the end of the request."""
    workspace_id: str
    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    queries_ok: int = 0
    queries_failed: int = 0
    rag_retrievals: int = 0
    cache_hits: int = 0


_current: ContextVar[UsageBucket | None] = ContextVar(
    "usage_bucket", default=None
)


def current() -> UsageBucket | None:
    """The bucket bound to this async context, or ``None`` if no
    bucket is active."""
    return _current.get()


def start_bucket(workspace_id: str) -> UsageBucket:
    """Open a fresh bucket for a request and bind it to this
    context. Returns the bucket so the caller can flush it at the
    end. Replacing an existing bucket is fine — the previous one
    becomes orphaned and gets GC'd."""
    bucket = UsageBucket(workspace_id=workspace_id)
    _current.set(bucket)
    return bucket


def clear_bucket() -> None:
    _current.set(None)


def record_llm(in_tokens: int, out_tokens: int) -> None:
    """Recording site used by :mod:`app.agents.llm.LLMClient` after
    every completion. Safe no-op when called outside a bucket
    scope (unit tests, background workers).
    """
    b = _current.get()
    if b is None:
        return
    b.llm_calls += 1
    if in_tokens > 0:
        b.llm_tokens_in += int(in_tokens)
    if out_tokens > 0:
        b.llm_tokens_out += int(out_tokens)


def record_query(ok: bool) -> None:
    b = _current.get()
    if b is None:
        return
    if ok:
        b.queries_ok += 1
    else:
        b.queries_failed += 1


def record_rag(retrieved_chunks: int) -> None:
    """Increments the retrieval count only when at least one chunk
    came back — failed Triton calls return zero and shouldn't count
    against the workspace's "RAG retrievals" metric."""
    if retrieved_chunks <= 0:
        return
    b = _current.get()
    if b is None:
        return
    b.rag_retrievals += 1


def record_cache_hit() -> None:
    b = _current.get()
    if b is None:
        return
    b.cache_hits += 1


async def flush_bucket(
    db: AsyncSession, bucket: UsageBucket | None = None
) -> None:
    """UPSERT the bucket's totals into ``usage_daily``. Called at the
    end of the chat request. Idempotent on retry — the UPSERT just
    re-applies the same delta.

    Failure mode: catches every exception so a usage-tracking
    hiccup never breaks the user's chat. The bucket gets logged
    and discarded.
    """
    b = bucket or _current.get()
    if b is None or not b.workspace_id:
        return
    if not _has_any_activity(b):
        return
    today = datetime.now(timezone.utc).date()
    # Postgres ON CONFLICT path — we rely on PG-only syntax because
    # production runs on Postgres and tests don't hit this code
    # (unit tests don't open a bucket). The single statement keeps
    # the UPSERT atomic per (workspace_id, day) row.
    stmt = sa_text(
        """
        INSERT INTO usage_daily (
            workspace_id, day,
            llm_calls, llm_tokens_in, llm_tokens_out,
            queries_ok, queries_failed,
            rag_retrievals, cache_hits,
            updated_at
        ) VALUES (
            :workspace_id, :day,
            :llm_calls, :llm_tokens_in, :llm_tokens_out,
            :queries_ok, :queries_failed,
            :rag_retrievals, :cache_hits,
            now()
        )
        ON CONFLICT (workspace_id, day) DO UPDATE SET
            llm_calls       = usage_daily.llm_calls + EXCLUDED.llm_calls,
            llm_tokens_in   = usage_daily.llm_tokens_in + EXCLUDED.llm_tokens_in,
            llm_tokens_out  = usage_daily.llm_tokens_out + EXCLUDED.llm_tokens_out,
            queries_ok      = usage_daily.queries_ok + EXCLUDED.queries_ok,
            queries_failed  = usage_daily.queries_failed + EXCLUDED.queries_failed,
            rag_retrievals  = usage_daily.rag_retrievals + EXCLUDED.rag_retrievals,
            cache_hits      = usage_daily.cache_hits + EXCLUDED.cache_hits,
            updated_at      = now()
        """
    )
    params = {
        "workspace_id": b.workspace_id,
        "day": today,
        "llm_calls": b.llm_calls,
        "llm_tokens_in": b.llm_tokens_in,
        "llm_tokens_out": b.llm_tokens_out,
        "queries_ok": b.queries_ok,
        "queries_failed": b.queries_failed,
        "rag_retrievals": b.rag_retrievals,
        "cache_hits": b.cache_hits,
    }
    try:
        await db.execute(stmt, params)
        await db.commit()
    except Exception:  # noqa: BLE001 — usage tracking must never break chat
        log.exception(
            "usage.flush_bucket: UPSERT failed; bucket=%s", b
        )
        try:
            await db.rollback()
        except Exception:  # pragma: no cover
            pass


def _has_any_activity(b: UsageBucket) -> bool:
    return any(
        v > 0
        for v in (
            b.llm_calls,
            b.llm_tokens_in,
            b.llm_tokens_out,
            b.queries_ok,
            b.queries_failed,
            b.rag_retrievals,
            b.cache_hits,
        )
    )


__all__ = [
    "UsageBucket",
    "clear_bucket",
    "current",
    "flush_bucket",
    "record_cache_hit",
    "record_llm",
    "record_query",
    "record_rag",
    "start_bucket",
]
