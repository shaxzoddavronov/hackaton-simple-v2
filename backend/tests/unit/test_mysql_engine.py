"""Smoke tests for the MysqlEngine adapter.

The real ``asyncmy`` driver is monkey-patched with a fake that mimics
the surface ``MysqlEngine`` touches:

  * ``asyncmy.connect(**kwargs)`` returns a connection.
  * ``conn.cursor()`` returns an async-context-manager cursor.
  * The cursor exposes ``execute``, ``fetchmany``, and ``description``.

We assert that:

  * ``validate_readonly`` delegates to the sqlglot validator (mysql dialect).
  * ``execute`` produces a tabular ``ResultSet`` from canned rows /
    description, refuses write SQL before touching the driver, and honors
    ``row_cap`` with a ``truncated`` flag.
  * The ``_mysql_dtype`` helper maps a handful of protocol type codes.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import asyncmy

from app.engines.mysql import MysqlEngine, _mysql_dtype


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeCursor:
    """Mimics the asyncmy cursor used by ``MysqlEngine.execute``.

    The cursor is its own async-context-manager: ``conn.cursor()`` returns
    this object directly and the engine then uses ``async with`` on it.
    """

    def __init__(self, rows: list[list[Any]], description: list[tuple]) -> None:
        # ``_pending`` is the SELECT result; the SET SESSION statements
        # the engine issues drain through ``execute`` but don't consume
        # rows, so we keep the full result available until the SELECT
        # is issued.
        self._rows = list(rows)
        self.description: list[tuple] | None = list(description)
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> None:
        self.executed.append(sql)

    async def fetchmany(self, n: int) -> list[list[Any]]:
        out = self._rows[:n]
        self._rows = self._rows[n:]
        return out

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur
        self.closed = False

    def cursor(self) -> _FakeCursor:
        # The cursor is its own async-context-manager — return it
        # directly so ``async with conn.cursor() as cur`` works.
        return self._cur

    async def close(self) -> None:
        self.closed = True


def _patch_connect(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    """Replace ``asyncmy.connect`` with an async shim returning ``conn``."""

    async def _fake_connect(**_kwargs: Any) -> _FakeConn:
        return conn

    monkeypatch.setattr(asyncmy, "connect", _fake_connect)


def _make_engine() -> MysqlEngine:
    src = SimpleNamespace(
        connection_meta={
            "host": "localhost",
            "port": 3306,
            "db_name": "shop",
            "user": "ro",
            "password": "x",
        },
        _credentials={},
    )
    return MysqlEngine(src)


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
    # description tuples: (name, type_code, ...).
    # 3 = LONG (bigint), 253 = string-ish.
    description = [("id", 3, None, None, None, None, None), ("name", 253, None, None, None, None, None)]
    cur = _FakeCursor(
        rows=[[1, "ali"], [2, "bobur"], [3, "carol"]],
        description=description,
    )
    conn = _FakeConn(cur)
    _patch_connect(monkeypatch, conn)

    engine = _make_engine()
    rs = await engine.execute("SELECT id, name FROM users")

    assert rs.columns == ["id", "name"]
    assert rs.row_count == 3
    assert rs.rows == [[1, "ali"], [2, "bobur"], [3, "carol"]]
    assert rs.dtypes[0] == "bigint"
    assert rs.dtypes[1] == "string"
    assert rs.truncated is False
    # Sanity check: the engine issued the session-readonly + timeout +
    # the user SQL through the same cursor.
    assert any("SELECT id, name FROM users" in s for s in cur.executed)
    assert any("READ ONLY" in s for s in cur.executed)


@pytest.mark.asyncio
async def test_execute_refuses_write_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the validator is bypassed, this shim would blow up on connect.
    async def _explode(**_kwargs: Any) -> None:
        raise AssertionError("driver should not have been called")

    monkeypatch.setattr(asyncmy, "connect", _explode)

    engine = _make_engine()
    with pytest.raises(ValueError):
        await engine.execute("DROP TABLE evil")


@pytest.mark.asyncio
async def test_execute_truncates_to_row_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = [("x", 3, None, None, None, None, None)]
    # row_cap=2 + the engine's "+1 to detect truncation" -> 3 rows is
    # already enough to trigger truncation, but we send 5 to be generous.
    cur = _FakeCursor(
        rows=[[1], [2], [3], [4], [5]],
        description=description,
    )
    _patch_connect(monkeypatch, _FakeConn(cur))

    engine = _make_engine()
    rs = await engine.execute("SELECT x FROM t", row_cap=2)
    assert rs.row_count == 2
    assert rs.truncated is True
    assert rs.rows == [[1], [2]]


# ── _mysql_dtype helper ─────────────────────────────────────────────


def test_dtype_helper_maps_common_types() -> None:
    assert _mysql_dtype(3) == "bigint"      # LONG
    assert _mysql_dtype(5) == "double"      # DOUBLE
    assert _mysql_dtype(246) == "numeric"   # NEWDECIMAL
    assert _mysql_dtype(12) == "timestamp"  # DATETIME
    assert _mysql_dtype(16) == "bool"       # BIT
    assert _mysql_dtype(999) == "string"    # unknown -> string fallback


# ── Constructor input validation ────────────────────────────────────


def test_missing_required_keys_raises() -> None:
    src = SimpleNamespace(connection_meta={"host": "localhost"}, _credentials={})
    with pytest.raises(ValueError, match="missing keys"):
        MysqlEngine(src)
