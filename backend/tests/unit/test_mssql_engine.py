"""Smoke tests for the MssqlEngine adapter.

The real ``aioodbc`` driver is monkey-patched with a fake that mimics
the surface ``MssqlEngine`` touches:

  * ``aioodbc.connect(dsn=..., autocommit=True)`` returns a connection.
  * ``conn.cursor()`` returns an async-context-manager cursor.
  * The cursor exposes ``execute``, ``fetchmany``, ``fetchall``,
    ``fetchone``, and ``description``.

We assert that:

  * ``validate_readonly`` delegates to the sqlglot validator (tsql).
  * ``execute`` produces a tabular ``ResultSet`` from canned rows /
    description, refuses write SQL before touching the driver, and
    honors ``row_cap`` with a ``truncated`` flag.
  * The registry returns an ``MssqlEngine`` instance for ``"mssql"``.
  * The ``_mssql_dtype`` helper maps DATA_TYPE strings to coarse
    buckets.
  * The DSN builder produces the expected ODBC connection string.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import aioodbc
import pytest

from app.engines import register_all
from app.engines.mssql import (
    MssqlEngine,
    _build_dsn,
    _mssql_dtype,
)
from app.engines.registry import get_engine


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeCursor:
    """Mimics the aioodbc cursor used by ``MssqlEngine.execute``.

    The cursor is its own async-context-manager: ``conn.cursor()`` returns
    this object directly and the engine then uses ``async with`` on it.
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

    async def fetchall(self) -> list[list[Any]]:
        out = self._rows[:]
        self._rows = []
        return out

    async def fetchone(self) -> list[Any] | None:
        if not self._rows:
            return None
        head = self._rows[0]
        self._rows = self._rows[1:]
        return head

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
    """Replace ``aioodbc.connect`` with an async shim returning ``conn``."""

    async def _fake_connect(**_kwargs: Any) -> _FakeConn:
        return conn

    monkeypatch.setattr(aioodbc, "connect", _fake_connect)


def _make_engine() -> MssqlEngine:
    src = SimpleNamespace(
        connection_meta={
            "host": "localhost",
            "port": 1433,
            "db_name": "shop",
            "user": "ro",
            "password": "x",
        },
        _credentials={},
    )
    return MssqlEngine(src)


# ── Registry ────────────────────────────────────────────────────────


def test_registry_returns_mssql_engine() -> None:
    register_all()
    src = SimpleNamespace(
        dialect="mssql",
        connection_meta={
            "host": "localhost",
            "port": 1433,
            "db_name": "shop",
            "user": "ro",
            "password": "x",
        },
        _credentials={},
    )
    eng = get_engine(src)
    assert isinstance(eng, MssqlEngine)
    assert eng.dialect == "mssql"


# ── validate_readonly ───────────────────────────────────────────────


def test_validate_readonly_accepts_select() -> None:
    engine = _make_engine()
    res = engine.validate_readonly("SELECT 1")
    assert res.ok
    # The sqlglot validator injects a row cap; for tsql it renders as
    # ``SELECT TOP 1000`` (proper T-SQL form).
    assert "TOP" in (res.rewritten_sql or "").upper()


def test_validate_readonly_rejects_drop() -> None:
    engine = _make_engine()
    assert not engine.validate_readonly("DROP TABLE t").ok


def test_validate_readonly_rejects_insert() -> None:
    engine = _make_engine()
    assert not engine.validate_readonly("INSERT INTO t VALUES (1)").ok


def test_validate_readonly_rejects_update() -> None:
    engine = _make_engine()
    assert not engine.validate_readonly("UPDATE t SET x = 1").ok


def test_validate_readonly_keeps_existing_top() -> None:
    engine = _make_engine()
    res = engine.validate_readonly("SELECT TOP 5 * FROM foo")
    assert res.ok
    # Existing TOP isn't doubled — the validator only injects when missing.
    sql = (res.rewritten_sql or "").upper()
    assert "TOP 5" in sql
    assert "TOP 1000" not in sql


# ── execute → ResultSet ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_returns_tabular_resultset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pyodbc description tuples: (name, type_code, display_size,
    # internal_size, precision, scale, null_ok). type_code is a Python
    # ``type`` object — ``int`` and ``str`` here.
    description = [
        ("id", int, None, None, None, None, True),
        ("name", str, None, None, None, None, True),
    ]
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
    assert rs.dtypes[1] == "varchar"
    assert rs.truncated is False
    # Sanity check: the engine issued the isolation-level + cost-limit +
    # the user SQL (after the validator's TOP rewrite) through the same
    # cursor.
    assert any("READ UNCOMMITTED" in s for s in cur.executed)
    assert any("QUERY_GOVERNOR_COST_LIMIT" in s for s in cur.executed)
    assert any("id, name FROM users" in s for s in cur.executed)
    assert any("TOP" in s.upper() for s in cur.executed)


@pytest.mark.asyncio
async def test_execute_refuses_write_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the validator is bypassed, this shim would blow up on connect.
    async def _explode(**_kwargs: Any) -> None:
        raise AssertionError("driver should not have been called")

    monkeypatch.setattr(aioodbc, "connect", _explode)

    engine = _make_engine()
    with pytest.raises(ValueError):
        await engine.execute("DROP TABLE evil")


@pytest.mark.asyncio
async def test_execute_truncates_to_row_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = [("x", int, None, None, None, None, True)]
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


@pytest.mark.asyncio
async def test_execute_coerces_decimal_to_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = [
        ("amount", Decimal, None, None, None, None, True),
    ]
    cur = _FakeCursor(
        rows=[[Decimal("12.50")], [Decimal("0.01")]],
        description=description,
    )
    _patch_connect(monkeypatch, _FakeConn(cur))

    engine = _make_engine()
    rs = await engine.execute("SELECT amount FROM payments")
    assert rs.rows == [[12.5], [0.01]]
    assert all(isinstance(r[0], float) for r in rs.rows)
    assert rs.dtypes[0] == "double"


# ── _mssql_dtype helper ─────────────────────────────────────────────


def test_dtype_helper_maps_common_types() -> None:
    assert _mssql_dtype("bigint") == "bigint"
    assert _mssql_dtype("int") == "bigint"
    assert _mssql_dtype("smallint") == "bigint"
    assert _mssql_dtype("tinyint") == "bigint"
    assert _mssql_dtype("float") == "double"
    assert _mssql_dtype("real") == "double"
    assert _mssql_dtype("decimal") == "decimal"
    assert _mssql_dtype("numeric") == "decimal"
    assert _mssql_dtype("money") == "decimal"
    assert _mssql_dtype("smallmoney") == "decimal"
    assert _mssql_dtype("nvarchar") == "varchar"
    assert _mssql_dtype("varchar") == "varchar"
    assert _mssql_dtype("nchar") == "text"
    assert _mssql_dtype("text") == "text"
    assert _mssql_dtype("ntext") == "text"
    assert _mssql_dtype("datetime") == "timestamp"
    assert _mssql_dtype("datetime2") == "timestamp"
    assert _mssql_dtype("smalldatetime") == "timestamp"
    assert _mssql_dtype("datetimeoffset") == "timestamp"
    assert _mssql_dtype("date") == "date"
    assert _mssql_dtype("bit") == "boolean"
    assert _mssql_dtype("uniqueidentifier") == "uuid"
    # Unknown fall-through.
    assert _mssql_dtype("xml") == "unknown"
    assert _mssql_dtype("") == "unknown"
    assert _mssql_dtype(None) == "unknown"


# ── DSN builder ─────────────────────────────────────────────────────


def test_build_dsn_default_driver() -> None:
    dsn = _build_dsn(
        {
            "host": "db1.example.com",
            "port": 1433,
            "db_name": "shop",
            "user": "ro",
            "password": "pw",
        }
    )
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in dsn
    assert "SERVER=db1.example.com,1433" in dsn
    assert "DATABASE=shop" in dsn
    assert "UID=ro" in dsn
    assert "PWD=pw" in dsn
    assert "TrustServerCertificate=yes" in dsn
    assert "Encrypt=optional" in dsn


def test_build_dsn_custom_driver_and_default_port() -> None:
    dsn = _build_dsn(
        {
            "host": "h",
            "db_name": "d",
            "user": "u",
            "password": "p",
            "driver": "ODBC Driver 17 for SQL Server",
        }
    )
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in dsn
    # Default port falls back to 1433.
    assert "SERVER=h,1433" in dsn


# ── Constructor input validation ────────────────────────────────────


def test_missing_required_keys_raises() -> None:
    src = SimpleNamespace(connection_meta={"host": "localhost"}, _credentials={})
    with pytest.raises(ValueError, match="missing keys"):
        MssqlEngine(src)


def test_credentials_dict_merged_with_meta() -> None:
    # Credentials sit on a separate dict in the WorkspaceConnection ORM
    # row; the engine merges them with connection_meta at construction.
    src = SimpleNamespace(
        connection_meta={"host": "h", "port": 1433, "db_name": "d"},
        _credentials={"user": "ro", "password": "pw"},
    )
    eng = MssqlEngine(src)
    assert eng.dialect == "mssql"
    assert "UID=ro" in eng._dsn
    assert "PWD=pw" in eng._dsn
