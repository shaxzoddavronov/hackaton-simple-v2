"""Smoke tests for the ClickhouseEngine adapter.

The real ``clickhouse_connect`` async client is monkey-patched with a
fake that mimics the surface ``ClickhouseEngine`` touches:

  * ``get_async_client(**kwargs)`` returns a client (we shim this in the
    ``app.engines.clickhouse`` module namespace, which is where the
    engine imported it).
  * ``await client.query(sql, settings=...)`` returns a result with
    ``column_names``, ``column_types``, ``result_rows``.

We assert that:

  * ``validate_readonly`` delegates to the sqlglot validator
    (clickhouse dialect).
  * ``execute`` produces a tabular ``ResultSet``, refuses write SQL
    before touching the driver, and honors ``row_cap`` with truncation.
  * ``_ch_dtype`` maps a handful of ClickHouse type names.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import app.engines.clickhouse as ch_module
from app.engines.clickhouse import ClickhouseEngine, _ch_dtype


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeType:
    """Stand-in for ``ClickHouseType`` — only ``.name`` is read."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeResult:
    def __init__(
        self,
        names: list[str],
        types: list[_FakeType],
        rows: list[list[Any]],
    ) -> None:
        self.column_names = names
        self.column_types = types
        self.result_rows = rows


class _FakeCHClient:
    def __init__(self, result: _FakeResult) -> None:
        self._result = result
        # Records every (sql, settings) tuple — handy for asserting that
        # the engine passed ``readonly=2`` / ``max_result_rows`` etc.
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def query(
        self,
        sql: str,
        parameters: dict | None = None,
        settings: dict | None = None,
    ) -> _FakeResult:
        self.queries.append((sql, dict(settings or {})))
        return self._result

    async def close(self) -> None:
        self.closed = True


def _patch_get_client(
    monkeypatch: pytest.MonkeyPatch, client: _FakeCHClient
) -> None:
    """Replace ``get_async_client`` with an async shim that returns ``client``."""

    async def _fake_get(**_kwargs: Any) -> _FakeCHClient:
        return client

    monkeypatch.setattr(ch_module, "get_async_client", _fake_get)


def _make_engine() -> ClickhouseEngine:
    src = SimpleNamespace(
        connection_meta={
            "host": "localhost",
            "port": 8123,
            "db_name": "analytics",
            "user": "default",
            "password": "",
        },
        _credentials={},
    )
    return ClickhouseEngine(src)


# ── validate_readonly ───────────────────────────────────────────────


def test_validate_readonly_accepts_select() -> None:
    engine = _make_engine()
    assert engine.validate_readonly("SELECT 1").ok


def test_validate_readonly_rejects_drop() -> None:
    engine = _make_engine()
    assert not engine.validate_readonly("DROP TABLE t").ok


# ── execute → ResultSet ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_returns_tabular_resultset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _FakeResult(
        names=["id", "name"],
        types=[_FakeType("UInt32"), _FakeType("String")],
        rows=[[1, "ali"], [2, "bobur"], [3, "carol"]],
    )
    client = _FakeCHClient(result)
    _patch_get_client(monkeypatch, client)

    engine = _make_engine()
    rs = await engine.execute("SELECT id, name FROM users")

    assert rs.columns == ["id", "name"]
    assert rs.row_count == 3
    assert rs.rows == [[1, "ali"], [2, "bobur"], [3, "carol"]]
    assert rs.dtypes == ["bigint", "string"]
    assert rs.truncated is False
    # The engine should have passed readonly=2 in settings.
    assert client.queries
    _sql, settings = client.queries[0]
    assert settings.get("readonly") == 2
    assert "max_result_rows" in settings


@pytest.mark.asyncio
async def test_execute_refuses_write_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _explode(**_kwargs: Any) -> None:
        raise AssertionError("driver should not have been called")

    monkeypatch.setattr(ch_module, "get_async_client", _explode)

    engine = _make_engine()
    with pytest.raises(ValueError):
        await engine.execute("DROP TABLE evil")


@pytest.mark.asyncio
async def test_execute_truncates_to_row_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _FakeResult(
        names=["x"],
        types=[_FakeType("UInt64")],
        # 5 rows; row_cap=2 -> truncation flag should fire.
        rows=[[1], [2], [3], [4], [5]],
    )
    _patch_get_client(monkeypatch, _FakeCHClient(result))

    engine = _make_engine()
    rs = await engine.execute("SELECT x FROM t", row_cap=2)
    assert rs.row_count == 2
    assert rs.truncated is True
    assert rs.rows == [[1], [2]]


# ── _ch_dtype helper ────────────────────────────────────────────────


def test_dtype_helper_maps_common_types() -> None:
    # The helper accepts either a type-like object with ``.name`` or a
    # raw string — exercise both shapes.
    assert _ch_dtype(_FakeType("UInt32")) == "bigint"
    assert _ch_dtype(_FakeType("Int64")) == "bigint"
    assert _ch_dtype(_FakeType("Float64")) == "double"
    assert _ch_dtype(_FakeType("Decimal(18, 4)")) == "numeric"
    assert _ch_dtype(_FakeType("DateTime")) == "timestamp"
    assert _ch_dtype(_FakeType("Date")) == "timestamp"
    assert _ch_dtype(_FakeType("Bool")) == "bool"
    assert _ch_dtype(_FakeType("String")) == "string"
    assert _ch_dtype("Float32") == "double"
    # Unknown / opaque types (UUID, IPv4, Array of scalars, Map) fall
    # through to the "string" bucket. We pick names that don't contain
    # any of the substring matches the helper looks for ("Int", "Float",
    # "Decimal", "Date", "Bool").
    assert _ch_dtype(_FakeType("UUID")) == "string"
    assert _ch_dtype(_FakeType("IPv4")) == "string"


# ── Constructor input validation ────────────────────────────────────


def test_missing_required_keys_raises() -> None:
    src = SimpleNamespace(connection_meta={"host": "localhost"}, _credentials={})
    with pytest.raises(ValueError, match="missing keys"):
        ClickhouseEngine(src)
