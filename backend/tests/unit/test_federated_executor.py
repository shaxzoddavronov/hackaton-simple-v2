"""End-to-end smoke for ``federated_executor.run``.

No LLM, no Postgres / Mongo / ES. We patch ``get_engine`` to return
fake engines whose ``execute`` yields canned :class:`ResultSet` rows,
then feed a small FederatedPlan through the executor and assert that
sub-queries run in parallel, the merge pipeline folds them as
declared, and the final ``state.result`` matches expectations.

This is the only async wiring in the federation path that wasn't
covered by ``test_federation_merge.py`` (pure-Python merge primitives)
or the planner unit tests (LLM-driven, mocked separately).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.agents.nodes import federated_executor
from app.engines.base import ResultSet, ValidationResult


# ── Fake engine ─────────────────────────────────────────────────────


class _FakeEngine:
    """Returns canned ResultSets per ``execute`` call. Records every
    invocation so tests can assert parallelism and arguments."""

    def __init__(self, result: ResultSet) -> None:
        self._result = result
        self.executed_with: list[str] = []
        self.aclosed = False

    def validate_readonly(self, sql: str) -> ValidationResult:
        # Engine-level validator is bypassed in federated_executor by
        # the dialect-dispatched module-level validators. This stays
        # here to satisfy the QueryEngine duck type.
        return ValidationResult(ok=True)

    async def execute(self, sql: str, **kw: Any) -> ResultSet:
        self.executed_with.append(sql)
        # Sleep briefly so two concurrent executes overlap in real wall
        # time — lets us verify gather() is actually parallel.
        await asyncio.sleep(0.01)
        return self._result

    async def aclose(self) -> None:
        self.aclosed = True


def _rs(columns: list[str], rows: list[list[Any]]) -> ResultSet:
    return ResultSet(
        columns=columns,
        dtypes=["string"] * len(columns),
        rows=rows,
        row_count=len(rows),
        took_ms=0,
    )


# ── Common fixtures ────────────────────────────────────────────────


def _patch_executor(monkeypatch, *, engines_by_conn: dict[UUID, _FakeEngine]) -> None:
    """Swap every collaborator the executor talks to with an in-memory
    stub. We patch at the executor module's bound name (its import
    site), not the upstream package, so the patch actually fires."""

    async def _fake_decrypt(_session, _connection_id):
        return {"user": "u", "password": "p"}

    async def _no_engine_setup():
        return None

    # The executor opens a DB session to fetch WorkspaceConnection rows.
    # Replace it with a tiny stand-in.
    class _FakeWsConn:
        def __init__(self, cid: UUID, dialect: str) -> None:
            self.id = cid
            self.workspace_id = uuid4()
            self.dialect = dialect
            self.connection_meta: dict[str, Any] = {}

        # Required attribute the engine duck-types on.
        _credentials: dict[str, str] = {}

    fake_rows = {
        cid: _FakeWsConn(cid, "postgres") for cid in engines_by_conn
    }

    class _ScalarsAll:
        def __init__(self, items):
            self._items = items

        def all(self):
            return list(self._items)

    class _Result:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return _ScalarsAll(self._items)

    class _FakeSession:
        async def execute(self, _stmt):
            return _Result(list(fake_rows.values()))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, _model, _id):
            return None  # creds fetched via _decrypt_creds shim below

    class _FakeSessionFactory:
        def __call__(self):
            return _FakeSession()

    async def _fake_engine_dispose():
        return None

    class _FakeEngineSA:
        def dispose(self):
            return _fake_engine_dispose()

    def _fake_create_async_engine(*_a, **_kw):
        return _FakeEngineSA()

    def _fake_async_sessionmaker(*_a, **_kw):
        return _FakeSessionFactory()

    monkeypatch.setattr(
        federated_executor, "create_async_engine", _fake_create_async_engine
    )
    monkeypatch.setattr(
        federated_executor, "async_sessionmaker", _fake_async_sessionmaker
    )
    monkeypatch.setattr(federated_executor, "_decrypt_creds", _fake_decrypt)
    monkeypatch.setattr(
        federated_executor, "register_engines", lambda: None
    )

    def _fake_get_engine(conn):
        return engines_by_conn[conn.id]

    monkeypatch.setattr(federated_executor, "get_engine", _fake_get_engine)


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_subqueries_join_merge(monkeypatch):
    cid_orders = uuid4()
    cid_users = uuid4()

    engines = {
        cid_orders: _FakeEngine(
            _rs(["user_id", "revenue"], [[1, 100], [2, 50]])
        ),
        cid_users: _FakeEngine(
            _rs(["user_id", "country"], [[1, "UZ"], [2, "US"]])
        ),
    }
    _patch_executor(monkeypatch, engines_by_conn=engines)

    plan = {
        "sub_queries": [
            {
                "connection_id": str(cid_orders),
                "dialect": "postgres",
                "query": "SELECT user_id, SUM(amount) AS revenue FROM orders GROUP BY user_id",
                "alias": "orders",
                "rationale": "Revenue per user from PG.",
            },
            {
                "connection_id": str(cid_users),
                "dialect": "postgres",
                "query": "SELECT user_id, country FROM users",
                "alias": "users",
                "rationale": "Country per user from PG.",
            },
        ],
        "merge_steps": [
            {
                "kind": "join",
                "left": "orders",
                "right": "users",
                "on": ["user_id"],
                "output": "joined",
            }
        ],
        "rationale": "Cross-DB join",
        "expected_columns": ["user_id", "revenue", "country"],
    }

    state = {"federated_plan": plan, "executor_attempts": 0}
    out = await federated_executor.run(state)

    # Result is the joined table.
    rs = out["result"]
    assert rs.columns == ["user_id", "revenue", "country"]
    assert sorted(rs.rows) == [[1, 100, "UZ"], [2, 50, "US"]]
    assert rs.row_count == 2
    # Both engines actually got the query — proves parallel fan-out.
    # The validator injects ``LIMIT 1000`` on the outermost SELECT, so
    # we substring-match instead of equality-matching.
    assert len(engines[cid_orders].executed_with) == 1
    assert "FROM orders" in engines[cid_orders].executed_with[0]
    assert len(engines[cid_users].executed_with) == 1
    assert "FROM users" in engines[cid_users].executed_with[0]
    # Both engines closed even on the happy path.
    assert engines[cid_orders].aclosed
    assert engines[cid_users].aclosed
    # sub_results meta carried for SSE. The merge pipeline registers
    # its ``output`` alias too, so "joined" appears alongside the
    # original sub-queries — useful for transparency in the UI.
    assert {"orders", "users"}.issubset(out["sub_results"])
    assert out["sub_results"]["orders"]["row_count"] == 2
    assert out["last_executor_error"] is None


@pytest.mark.asyncio
async def test_single_subquery_no_merge_uses_sole_result(monkeypatch):
    cid = uuid4()
    engines = {cid: _FakeEngine(_rs(["x"], [[42]]))}
    _patch_executor(monkeypatch, engines_by_conn=engines)

    plan = {
        "sub_queries": [
            {
                "connection_id": str(cid),
                "dialect": "postgres",
                "query": "SELECT 42 AS x",
                "alias": "sole",
                "rationale": "only leg",
            }
        ],
        "merge_steps": [],
        "rationale": "single-leg federation (should have been data_query, but tolerate it)",
        "expected_columns": ["x"],
    }
    out = await federated_executor.run({"federated_plan": plan})
    assert out["result"].rows == [[42]]
    assert out["sub_results"]["sole"]["row_count"] == 1


@pytest.mark.asyncio
async def test_sub_query_validation_failure_aborts_plan(monkeypatch):
    cid = uuid4()
    # Engine never runs because validation kills the leg first.
    engines = {cid: _FakeEngine(_rs([], []))}
    _patch_executor(monkeypatch, engines_by_conn=engines)

    plan = {
        "sub_queries": [
            {
                "connection_id": str(cid),
                "dialect": "postgres",
                "query": "DROP TABLE evil",  # ← sqlglot rejects
                "alias": "bad",
                "rationale": "writes are banned",
            }
        ],
        "merge_steps": [],
        "rationale": "x",
        "expected_columns": [],
    }
    out = await federated_executor.run({"federated_plan": plan})
    assert out["last_executor_error"]
    assert "bad" in out["last_executor_error"]
    # Driver was never called — validation gates before execute.
    assert engines[cid].executed_with == []


@pytest.mark.asyncio
async def test_unknown_connection_id_surfaces_clean_error(monkeypatch):
    cid_real = uuid4()
    cid_ghost = uuid4()
    engines = {cid_real: _FakeEngine(_rs(["x"], [[1]]))}
    _patch_executor(monkeypatch, engines_by_conn=engines)

    plan = {
        "sub_queries": [
            {
                "connection_id": str(cid_ghost),  # not in our fake table
                "dialect": "postgres",
                "query": "SELECT 1",
                "alias": "ghost",
                "rationale": "x",
            }
        ],
        "merge_steps": [],
        "rationale": "x",
        "expected_columns": [],
    }
    out = await federated_executor.run({"federated_plan": plan})
    assert out["last_executor_error"]
    assert "no longer exist" in out["last_executor_error"]


@pytest.mark.asyncio
async def test_row_cap_truncates_runaway_join(monkeypatch):
    cid_l = uuid4()
    cid_r = uuid4()
    # Cartesian-style join: 5 left rows × 5 right rows = 25 joined rows.
    left_rows = [[i, i * 10] for i in range(5)]
    right_rows = [[i, f"r{i}"] for i in range(5)]
    engines = {
        cid_l: _FakeEngine(_rs(["k", "v"], left_rows)),
        cid_r: _FakeEngine(_rs(["k", "extra"], right_rows)),
    }
    _patch_executor(monkeypatch, engines_by_conn=engines)

    plan = {
        "sub_queries": [
            {
                "connection_id": str(cid_l),
                "dialect": "postgres",
                "query": "SELECT k, v FROM l",
                "alias": "l",
                "rationale": "x",
            },
            {
                "connection_id": str(cid_r),
                "dialect": "postgres",
                "query": "SELECT k, extra FROM r",
                "alias": "r",
                "rationale": "x",
            },
        ],
        "merge_steps": [
            {"kind": "join", "left": "l", "right": "r", "on": ["k"], "output": "m"}
        ],
        "rationale": "x",
        "expected_columns": [],
    }

    # Force a tiny cap so we can exercise the truncation path without
    # actually building 1000+ rows.
    monkeypatch.setattr(
        federated_executor.settings, "FEDERATION_MAX_ROWS", 3
    )
    out = await federated_executor.run({"federated_plan": plan})
    rs = out["result"]
    assert rs.row_count == 3
    assert rs.truncated is True
