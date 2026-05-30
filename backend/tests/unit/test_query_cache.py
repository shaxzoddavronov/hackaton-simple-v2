"""Phase 23 — Redis-backed query result cache.

We mock the redis client so the tests don't need a running server.
Coverage: key normalisation, hit/miss/skip semantics, size + row
caps, connection-scoped invalidation, Redis outage degrades
gracefully.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.engines.base import ResultSet
from app.services import query_cache


# ── _normalise_sql + _key ────────────────────────────────────────


def test_normalise_collapses_whitespace() -> None:
    a = query_cache._normalise_sql("SELECT  *\n  FROM  users")
    b = query_cache._normalise_sql("SELECT * FROM users")
    assert a == b == "SELECT * FROM users"


def test_normalise_handles_empty_input() -> None:
    assert query_cache._normalise_sql("") == ""
    assert query_cache._normalise_sql(None) == ""  # type: ignore[arg-type]


def test_key_is_stable_for_equivalent_sql() -> None:
    k1 = query_cache._key("c1", "SELECT * FROM users")
    k2 = query_cache._key("c1", "SELECT  *  FROM  users")
    assert k1 == k2


def test_key_is_unique_per_connection() -> None:
    k1 = query_cache._key("c1", "SELECT 1")
    k2 = query_cache._key("c2", "SELECT 1")
    assert k1 != k2


# ── _redis_client gating ─────────────────────────────────────────


def test_redis_client_returns_none_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        query_cache.settings, "QUERY_CACHE_ENABLED", False
    )
    assert query_cache._redis_client() is None


def test_redis_client_returns_none_on_import_failure(monkeypatch) -> None:
    """If ``redis`` raises during init, return None and log — don't
    crash the executor."""
    import sys

    monkeypatch.setattr(
        query_cache.settings, "QUERY_CACHE_ENABLED", True
    )
    # Replace the redis module with one whose Redis.from_url
    # raises.
    fake_mod = MagicMock()
    fake_mod.Redis.from_url.side_effect = RuntimeError("connection refused")
    with patch.dict(sys.modules, {"redis": fake_mod}):
        assert query_cache._redis_client() is None


# ── get_cached / set_cached ──────────────────────────────────────


def _rs(row_count: int = 2) -> ResultSet:
    return ResultSet(
        columns=["id", "name"],
        dtypes=["bigint", "text"],
        rows=[[i, f"row {i}"] for i in range(row_count)],
        row_count=row_count,
        truncated=False,
        took_ms=1,
    )


@pytest.mark.asyncio
async def test_get_cached_miss_returns_none(monkeypatch) -> None:
    fake_client = MagicMock()
    fake_client.get.return_value = None
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )
    assert await query_cache.get_cached("c1", "SELECT 1") is None


@pytest.mark.asyncio
async def test_set_and_get_roundtrip(monkeypatch) -> None:
    """Fake redis returns the last stored payload on GET."""
    store: dict[str, bytes] = {}
    fake_client = MagicMock()
    fake_client.set.side_effect = (
        lambda k, v, ex: store.__setitem__(k, v)
    )
    fake_client.get.side_effect = lambda k: store.get(k)
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )

    rs = _rs(row_count=3)
    assert await query_cache.set_cached("c1", "SELECT * FROM t", rs)

    got = await query_cache.get_cached("c1", "SELECT * FROM t")
    assert got is not None
    assert got.columns == rs.columns
    assert got.row_count == 3
    assert got.rows[0][1] == "row 0"


@pytest.mark.asyncio
async def test_set_skips_large_row_count(monkeypatch) -> None:
    fake_client = MagicMock()
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )
    monkeypatch.setattr(
        query_cache.settings, "QUERY_CACHE_MAX_ROWS", 10
    )
    rs = _rs(row_count=50)
    assert not await query_cache.set_cached("c1", "SELECT 1", rs)
    fake_client.set.assert_not_called()


@pytest.mark.asyncio
async def test_set_skips_oversized_payload(monkeypatch) -> None:
    fake_client = MagicMock()
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )
    # 100 bytes cap → any ResultSet exceeds it.
    monkeypatch.setattr(
        query_cache.settings, "QUERY_CACHE_MAX_BYTES", 100
    )
    rs = _rs(row_count=5)
    assert not await query_cache.set_cached("c1", "SELECT 1", rs)
    fake_client.set.assert_not_called()


@pytest.mark.asyncio
async def test_get_redis_failure_returns_none(monkeypatch) -> None:
    fake_client = MagicMock()
    fake_client.get.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )
    assert await query_cache.get_cached("c1", "SELECT 1") is None


@pytest.mark.asyncio
async def test_set_redis_failure_returns_false(monkeypatch) -> None:
    fake_client = MagicMock()
    fake_client.set.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )
    assert not await query_cache.set_cached("c1", "SELECT 1", _rs())


@pytest.mark.asyncio
async def test_get_corrupt_payload_returns_none(monkeypatch) -> None:
    fake_client = MagicMock()
    fake_client.get.return_value = b"{not valid json"
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )
    assert await query_cache.get_cached("c1", "SELECT 1") is None


@pytest.mark.asyncio
async def test_cache_disabled_no_op(monkeypatch) -> None:
    monkeypatch.setattr(
        query_cache.settings, "QUERY_CACHE_ENABLED", False
    )
    assert await query_cache.get_cached("c1", "SELECT 1") is None
    assert not await query_cache.set_cached("c1", "SELECT 1", _rs())


# ── invalidate_connection ────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_drops_only_matching_prefix(monkeypatch) -> None:
    """SCAN over the connection-scoped prefix; matching keys are
    DELETEd, others untouched."""
    deleted: list[Any] = []
    fake_client = MagicMock()
    # Two keys for c1, one for c2 — invalidate_connection("c1")
    # should remove only the two.
    keys_for_c1 = [
        b"qm:qcache:c1:hash-a",
        b"qm:qcache:c1:hash-b",
    ]
    fake_client.scan_iter.return_value = iter(keys_for_c1)
    fake_client.delete.side_effect = lambda k: deleted.append(k)
    monkeypatch.setattr(
        query_cache, "_redis_client", lambda: fake_client
    )

    removed = await query_cache.invalidate_connection("c1")
    assert removed == 2
    assert deleted == keys_for_c1
    # Confirm we scoped the SCAN to the right prefix.
    args, kwargs = fake_client.scan_iter.call_args
    assert "c1" in kwargs["match"]
    assert "qm:qcache" in kwargs["match"]


@pytest.mark.asyncio
async def test_invalidate_redis_down_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(query_cache, "_redis_client", lambda: None)
    assert await query_cache.invalidate_connection("c1") == 0
