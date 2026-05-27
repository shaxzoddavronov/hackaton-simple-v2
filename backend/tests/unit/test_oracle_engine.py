"""Smoke tests for the OracleEngine adapter.

The real ``oracledb`` driver is monkey-patched with a fake that mimics
the surface ``OracleEngine`` touches:

  * ``oracledb.connect_async(**kwargs)`` returns a connection (the
    engine has a ``to_thread(oracledb.connect, ...)`` fallback for
    builds that lack ``connect_async``; modern oracledb 2.x+ exposes the
    native async API, so we exercise that path).
  * ``conn.cursor()`` returns an async-context-manager cursor.
  * The cursor exposes ``execute``, ``fetchmany``, ``fetchone``, and
    ``description``.
  * ``conn.call_timeout`` is an integer attribute (the engine assigns to it).

We assert that:

  * ``validate_readonly`` delegates to the sqlglot validator
    (oracle dialect).
  * ``execute`` produces a tabular ``ResultSet``, refuses write SQL
    before touching the driver, and honors ``row_cap`` with truncation.
  * ``_oracle_dtype`` maps a handful of ``oracledb.DB_TYPE_*`` constants
    when fed description-shaped tuples.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import oracledb

from app.engines.oracle import OracleEngine, _oracle_dtype


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeCursor:
    """Mimics the oracledb async cursor used by ``OracleEngine.execute``.

    The cursor is its own async-context-manager: ``conn.cursor()`` returns
    this object and the engine then uses ``async with`` on it.
    """

    def __init__(self, rows: list[list[Any]], description: list[tuple]) -> None:
        self._rows = list(rows)
        self.description: list[tuple] | None = list(description)
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> None:
        self.executed.append(sql)

    async def fetchmany(self, n: int) -> list[list[Any]]:
        out = self._rows[:n]
        self._rows = self._rows[n:]
        return out

    async def fetchone(self) -> list[Any] | None:
        if not self._rows:
            return None
        first = self._rows[0]
        self._rows = self._rows[1:]
        return first

    async def fetchall(self) -> list[list[Any]]:
        out = self._rows
        self._rows = []
        return out

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur
        # The engine sets ``conn.call_timeout = timeout_s * 1000`` before
        # opening the cursor — keep it as a plain attribute.
        self.call_timeout: int = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cur

    async def close(self) -> None:
        self.closed = True


def _patch_connect(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    """Replace ``oracledb.connect_async`` with an async shim returning ``conn``."""

    async def _fake_connect_async(**_kwargs: Any) -> _FakeConn:
        return conn

    monkeypatch.setattr(oracledb, "connect_async", _fake_connect_async)


def _make_engine() -> OracleEngine:
    src = SimpleNamespace(
        connection_meta={
            "host": "localhost",
            "port": 1521,
            "db_name": "ORCLPDB1",
            "user": "ro",
            "password": "x",
        },
        _credentials={},
    )
    return OracleEngine(src)


# ── validate_readonly ───────────────────────────────────────────────


def test_validate_readonly_accepts_select() -> None:
    engine = _make_engine()
    assert engine.validate_readonly("SELECT 1 FROM DUAL").ok


def test_validate_readonly_rejects_drop() -> None:
    engine = _make_engine()
    assert not engine.validate_readonly("DROP TABLE t").ok


# ── execute → ResultSet ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_returns_tabular_resultset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cursor.description tuple shape: (name, type_code, display_size,
    # internal_size, precision, scale, null_ok). Only [0] (name) and [1]
    # (type code) are read by the engine.
    description = [
        ("ID", oracledb.DB_TYPE_NUMBER, None, None, None, None, None),
        ("NAME", oracledb.DB_TYPE_VARCHAR, None, None, None, None, None),
    ]
    cur = _FakeCursor(
        rows=[[1, "ali"], [2, "bobur"], [3, "carol"]],
        description=description,
    )
    conn = _FakeConn(cur)
    _patch_connect(monkeypatch, conn)

    engine = _make_engine()
    rs = await engine.execute("SELECT id, name FROM users")

    assert rs.columns == ["ID", "NAME"]
    assert rs.row_count == 3
    assert rs.rows == [[1, "ali"], [2, "bobur"], [3, "carol"]]
    # NUMBER -> "double" per the oracle dtype map; VARCHAR -> "string".
    assert rs.dtypes == ["double", "string"]
    assert rs.truncated is False
    # The engine should have set call_timeout (ms) and issued the
    # SET TRANSACTION READ ONLY statement.
    assert conn.call_timeout > 0
    assert any("SET TRANSACTION READ ONLY" in s for s in cur.executed)
    assert any("SELECT id, name FROM users" in s for s in cur.executed)


@pytest.mark.asyncio
async def test_execute_refuses_write_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _explode(**_kwargs: Any) -> None:
        raise AssertionError("driver should not have been called")

    monkeypatch.setattr(oracledb, "connect_async", _explode)

    engine = _make_engine()
    with pytest.raises(ValueError):
        await engine.execute("DROP TABLE evil")


@pytest.mark.asyncio
async def test_execute_truncates_to_row_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = [("X", oracledb.DB_TYPE_NUMBER, None, None, None, None, None)]
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


# ── _oracle_dtype helper ────────────────────────────────────────────


def test_dtype_helper_maps_common_types() -> None:
    # ``_oracle_dtype`` reads index 1 of a description-shaped tuple.
    def desc(code: Any) -> tuple:
        return ("col", code, None, None, None, None, None)

    assert _oracle_dtype(desc(oracledb.DB_TYPE_NUMBER)) == "double"
    assert _oracle_dtype(desc(oracledb.DB_TYPE_BINARY_DOUBLE)) == "double"
    assert _oracle_dtype(desc(oracledb.DB_TYPE_VARCHAR)) == "string"
    assert _oracle_dtype(desc(oracledb.DB_TYPE_CHAR)) == "string"
    assert _oracle_dtype(desc(oracledb.DB_TYPE_CLOB)) == "string"
    assert _oracle_dtype(desc(oracledb.DB_TYPE_DATE)) == "timestamp"
    assert _oracle_dtype(desc(oracledb.DB_TYPE_TIMESTAMP)) == "timestamp"
    assert _oracle_dtype(desc(oracledb.DB_TYPE_BOOLEAN)) == "bool"
    # Unknown code -> string fallback.
    assert _oracle_dtype(desc("UNKNOWN_TYPE_SENTINEL")) == "string"


# ── Constructor input validation ────────────────────────────────────


def test_missing_required_keys_raises() -> None:
    src = SimpleNamespace(connection_meta={"host": "localhost"}, _credentials={})
    with pytest.raises(ValueError, match="missing keys"):
        OracleEngine(src)


def test_service_name_aliases_db_name() -> None:
    """Oracle-native callers pass ``service_name``; the engine aliases it
    to the repo-wide ``db_name`` key. Smoke check that the constructor
    accepts that shape."""
    src = SimpleNamespace(
        connection_meta={
            "host": "localhost",
            "port": 1521,
            "service_name": "ORCLPDB1",
            "user": "ro",
            "password": "x",
        },
        _credentials={},
    )
    eng = OracleEngine(src)
    assert eng.dialect == "oracle"
