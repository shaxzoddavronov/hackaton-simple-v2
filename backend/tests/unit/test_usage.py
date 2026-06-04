"""Phase 37 — usage bucket + recording-site tests.

The DB flush path requires Postgres (ON CONFLICT) so we don't
exercise it in unit tests — it's covered by the e2e Postgres
fixture. Here we lock down the in-memory bucket semantics and the
no-op fallback when no bucket is bound.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.usage import (
    UsageBucket,
    clear_bucket,
    current,
    record_cache_hit,
    record_llm,
    record_query,
    record_rag,
    start_bucket,
)


@pytest.fixture(autouse=True)
def _reset_bucket():
    """Ensure no test leaks an active bucket into the next one."""
    clear_bucket()
    yield
    clear_bucket()


# ── start / current / clear ─────────────────────────────────


def test_current_returns_none_before_start() -> None:
    assert current() is None


def test_start_bucket_returns_and_binds_bucket() -> None:
    b = start_bucket("ws-1")
    assert isinstance(b, UsageBucket)
    assert b.workspace_id == "ws-1"
    assert current() is b


def test_clear_bucket_unbinds() -> None:
    start_bucket("ws-1")
    clear_bucket()
    assert current() is None


def test_start_replaces_existing_bucket() -> None:
    b1 = start_bucket("ws-1")
    b2 = start_bucket("ws-2")
    assert b1 is not b2
    assert current() is b2
    # b1 still exists in memory but is orphaned.
    assert b1.workspace_id == "ws-1"


# ── recording sites without bucket are no-ops ────────────────


def test_record_llm_without_bucket_is_noop() -> None:
    record_llm(in_tokens=100, out_tokens=50)  # should not raise
    assert current() is None


def test_record_query_without_bucket_is_noop() -> None:
    record_query(ok=True)
    assert current() is None


def test_record_rag_without_bucket_is_noop() -> None:
    record_rag(retrieved_chunks=5)
    assert current() is None


def test_record_cache_hit_without_bucket_is_noop() -> None:
    record_cache_hit()
    assert current() is None


# ── recording sites with bucket ──────────────────────────────


def test_record_llm_increments_counters() -> None:
    b = start_bucket("ws-1")
    record_llm(in_tokens=100, out_tokens=50)
    record_llm(in_tokens=200, out_tokens=80)
    assert b.llm_calls == 2
    assert b.llm_tokens_in == 300
    assert b.llm_tokens_out == 130


def test_record_llm_ignores_negative_tokens() -> None:
    """vLLM occasionally reports zero or missing usage; we still
    bump call count but not bogus token deltas."""
    b = start_bucket("ws-1")
    record_llm(in_tokens=0, out_tokens=0)
    record_llm(in_tokens=-5, out_tokens=-10)
    assert b.llm_calls == 2
    assert b.llm_tokens_in == 0
    assert b.llm_tokens_out == 0


def test_record_query_separates_ok_from_failed() -> None:
    b = start_bucket("ws-1")
    record_query(ok=True)
    record_query(ok=True)
    record_query(ok=False)
    assert b.queries_ok == 2
    assert b.queries_failed == 1


def test_record_rag_only_counts_when_chunks_returned() -> None:
    b = start_bucket("ws-1")
    record_rag(0)
    record_rag(-1)
    assert b.rag_retrievals == 0
    record_rag(7)
    assert b.rag_retrievals == 1


def test_record_cache_hit_increments() -> None:
    b = start_bucket("ws-1")
    record_cache_hit()
    record_cache_hit()
    assert b.cache_hits == 2


# ── async isolation: buckets are per-task ────────────────────


@pytest.mark.asyncio
async def test_buckets_are_isolated_across_tasks() -> None:
    """ContextVar gives each asyncio task its own copy. A bucket
    opened in one coroutine must not leak into a sibling
    coroutine."""

    results: dict[str, UsageBucket | None] = {}

    async def task(name: str) -> None:
        start_bucket(name)
        # Yield to give the other task a chance to also start a
        # bucket — we want to be sure they don't trample each other.
        await asyncio.sleep(0)
        record_llm(10, 5)
        results[name] = current()

    await asyncio.gather(task("ws-a"), task("ws-b"))

    a = results["ws-a"]
    b = results["ws-b"]
    assert a is not None and b is not None
    assert a is not b
    assert a.workspace_id == "ws-a"
    assert b.workspace_id == "ws-b"
    assert a.llm_calls == 1
    assert b.llm_calls == 1


# ── _has_any_activity through flush_bucket (unit-safe path) ──


@pytest.mark.asyncio
async def test_flush_bucket_skips_empty_bucket() -> None:
    """An empty bucket must NOT issue an UPSERT — the chat path
    opens a bucket on every request even for chitchat turns that
    never trigger any counter."""
    from app.services.usage import flush_bucket

    b = start_bucket("ws-1")

    class _Recorder:
        def __init__(self) -> None:
            self.executes: list = []

        async def execute(self, *args, **kwargs):
            self.executes.append((args, kwargs))

        async def commit(self):
            pass

        async def rollback(self):
            pass

    db = _Recorder()
    await flush_bucket(db, b)  # type: ignore[arg-type]
    assert db.executes == []  # no SQL ran


@pytest.mark.asyncio
async def test_flush_bucket_swallows_db_errors() -> None:
    """Usage tracking must never break the chat path. A DB error in
    the UPSERT becomes a logged warning, not an exception."""
    from app.services.usage import flush_bucket

    b = start_bucket("ws-1")
    record_llm(100, 50)

    class _Broken:
        def __init__(self) -> None:
            self.rolled_back = False

        async def execute(self, *args, **kwargs):
            raise RuntimeError("DB exploded")

        async def commit(self):
            pass

        async def rollback(self):
            self.rolled_back = True

    db = _Broken()
    # Should NOT raise.
    await flush_bucket(db, b)  # type: ignore[arg-type]
    assert db.rolled_back is True
