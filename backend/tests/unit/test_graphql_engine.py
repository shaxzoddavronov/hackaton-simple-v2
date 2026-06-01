"""Phase 32 — GraphQL engine + validator unit tests.

Covers:
  - validator rejects mutations / subscriptions / multi-op docs
  - validator rejects malformed envelopes
  - validator accepts plain queries (anonymous and named)
  - introspection produces a SchemaBundle from a mocked __schema reply
  - execute() POSTs the right body and flattens Relay edges to rows
  - auth headers (bearer / api_key header+query / basic) are injected
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.engines.graphql import (
    GraphqlEngine,
    _extract_rows_from_graphql,
    _flatten_graphql_rows,
    _unwrap_type_name,
)
from app.services.graphql_readonly_validator import validate_graphql_query


def _source(
    *,
    connection_meta: dict,
    credentials: dict | None = None,
    auth_kind: str = "none",
):
    return SimpleNamespace(
        dialect="graphql",
        connection_meta=connection_meta,
        _credentials=credentials or {},
        auth_kind=auth_kind,
    )


# ── helpers ──────────────────────────────────────────────────────


def test_unwrap_type_name_drills_non_null_list() -> None:
    type_ref = {
        "name": None,
        "kind": "NON_NULL",
        "ofType": {
            "name": None,
            "kind": "LIST",
            "ofType": {"name": "User", "kind": "OBJECT"},
        },
    }
    assert _unwrap_type_name(type_ref) == "User"


def test_unwrap_type_name_handles_named_at_top() -> None:
    assert _unwrap_type_name({"name": "Int", "kind": "SCALAR"}) == "Int"


def test_extract_rows_prefers_top_level_list() -> None:
    data = {"users": [{"id": 1}, {"id": 2}], "count": 99}
    assert _extract_rows_from_graphql(data) == [{"id": 1}, {"id": 2}]


def test_extract_rows_unwraps_relay_edges() -> None:
    data = {
        "repos": {
            "edges": [
                {"node": {"id": "a"}},
                {"node": {"id": "b"}},
            ]
        }
    }
    assert _extract_rows_from_graphql(data) == [{"id": "a"}, {"id": "b"}]


def test_extract_rows_falls_back_to_single_row() -> None:
    data = {"user": {"id": 1, "name": "x"}}
    rows = _extract_rows_from_graphql(data)
    assert rows == [{"user": {"id": 1, "name": "x"}}]


def test_flatten_rows_picks_scalar_columns() -> None:
    rows = [
        {"id": 1, "name": "a", "tags": ["x"]},
        {"id": 2, "name": "b", "tags": ["y", "z"]},
    ]
    cols, dtypes, out = _flatten_graphql_rows(rows, row_cap=100)
    assert cols == ["id", "name"]  # tags dropped because it's a list
    assert dtypes == ["bigint", "text"]
    assert out == [[1, "a"], [2, "b"]]


def test_flatten_rows_caps_at_row_cap() -> None:
    rows = [{"id": i} for i in range(10)]
    cols, _, out = _flatten_graphql_rows(rows, row_cap=3)
    assert len(out) == 3
    assert cols == ["id"]


# ── validator ────────────────────────────────────────────────────


def test_validator_accepts_anonymous_query() -> None:
    env = json.dumps({"query": "{ viewer { login } }"})
    result, parsed = validate_graphql_query(env)
    assert result.ok, result.findings
    assert parsed["query"].startswith("{")


def test_validator_accepts_named_query_with_variables() -> None:
    env = json.dumps(
        {
            "query": "query GetUser($id: ID!) { user(id: $id) { name } }",
            "variables": {"id": "42"},
        }
    )
    result, _ = validate_graphql_query(env)
    assert result.ok


def test_validator_rejects_mutation() -> None:
    env = json.dumps(
        {
            "query": "mutation DeleteAll { deleteEverything { ok } }"
        }
    )
    result, _ = validate_graphql_query(env)
    assert not result.ok
    assert any(
        f.code == "graphql_write_operation" for f in result.findings
    )


def test_validator_rejects_subscription() -> None:
    env = json.dumps(
        {"query": "subscription S { onChange { id } }"}
    )
    result, _ = validate_graphql_query(env)
    assert not result.ok
    assert any(
        f.code == "graphql_write_operation" for f in result.findings
    )


def test_validator_rejects_mixed_doc_with_mutation() -> None:
    env = json.dumps(
        {
            "query": (
                "query Q { a } "
                "mutation M { writeStuff { ok } }"
            )
        }
    )
    result, _ = validate_graphql_query(env)
    assert not result.ok


def test_validator_rejects_invalid_json() -> None:
    result, env = validate_graphql_query("{not json")
    assert not result.ok
    assert result.findings[0].code == "graphql_invalid_envelope"
    assert env is None


def test_validator_rejects_non_object_envelope() -> None:
    result, _ = validate_graphql_query(json.dumps([1, 2, 3]))
    assert not result.ok
    assert result.findings[0].code == "graphql_invalid_envelope"


def test_validator_rejects_missing_query() -> None:
    result, _ = validate_graphql_query(json.dumps({"variables": {}}))
    assert not result.ok
    assert result.findings[0].code == "graphql_missing_query"


def test_validator_rejects_empty_query() -> None:
    result, _ = validate_graphql_query(json.dumps({"query": "   "}))
    assert not result.ok
    assert result.findings[0].code == "graphql_missing_query"


def test_validator_rejects_bad_variables_type() -> None:
    env = json.dumps({"query": "{a}", "variables": "not-a-dict"})
    result, _ = validate_graphql_query(env)
    assert not result.ok
    assert result.findings[0].code == "graphql_bad_variables"


def test_validator_rejects_unparseable_query() -> None:
    env = json.dumps({"query": "query { unbalanced { "})
    result, _ = validate_graphql_query(env)
    assert not result.ok
    assert result.findings[0].code == "graphql_parse_error"


def test_validator_rejects_fragment_only_doc() -> None:
    env = json.dumps(
        {"query": "fragment F on User { id name }"}
    )
    result, _ = validate_graphql_query(env)
    assert not result.ok
    assert result.findings[0].code == "graphql_no_operation"


# ── construction guard rails ────────────────────────────────────


def test_engine_requires_endpoint() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        GraphqlEngine(_source(connection_meta={}))


def test_engine_rejects_non_http_endpoint() -> None:
    with pytest.raises(ValueError, match="http"):
        GraphqlEngine(
            _source(connection_meta={"endpoint": "ftp://x.example/q"})
        )


# ── introspection (mocked) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_introspect_builds_schema_bundle(monkeypatch) -> None:
    introspection_reply = {
        "data": {
            "__schema": {
                "queryType": {
                    "name": "Query",
                    "fields": [
                        {
                            "name": "users",
                            "description": "list users",
                            "args": [],
                            "type": {
                                "name": None,
                                "kind": "LIST",
                                "ofType": {
                                    "name": "User",
                                    "kind": "OBJECT",
                                },
                            },
                        },
                        {
                            "name": "__type",  # introspection field
                            "args": [],
                            "type": {"name": "String", "kind": "SCALAR"},
                        },
                    ],
                },
                "types": [
                    {
                        "name": "User",
                        "kind": "OBJECT",
                        "fields": [
                            {
                                "name": "id",
                                "type": {"name": "ID", "kind": "SCALAR"},
                            },
                            {
                                "name": "login",
                                "type": {
                                    "name": "String",
                                    "kind": "SCALAR",
                                },
                            },
                            {
                                "name": "__typename",
                                "type": {
                                    "name": "String",
                                    "kind": "SCALAR",
                                },
                            },
                        ],
                    }
                ],
            }
        }
    }

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=introspection_reply)
    )

    eng = GraphqlEngine(
        _source(
            connection_meta={"endpoint": "https://api.example/graphql"}
        )
    )
    # Patch the AsyncClient ctor used inside introspect_schema.
    import app.engines.graphql as gqm

    orig = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(gqm.httpx, "AsyncClient", _patched)
    bundle = await eng.introspect_schema()
    assert bundle.dialect == "graphql"
    assert [t.name for t in bundle.tables] == ["users"]
    cols = {c.name for c in bundle.tables[0].columns}
    # __typename should be filtered out
    assert cols == {"id", "login"}


# ── execute (mocked) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_flattens_relay_edges(monkeypatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content.decode())
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "data": {
                    "repositories": {
                        "edges": [
                            {"node": {"id": "1", "stars": 50}},
                            {"node": {"id": "2", "stars": 99}},
                        ]
                    }
                }
            },
        )

    transport = httpx.MockTransport(handler)
    import app.engines.graphql as gqm

    orig = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(gqm.httpx, "AsyncClient", _patched)
    eng = GraphqlEngine(
        _source(
            connection_meta={"endpoint": "https://api.example/graphql"},
            credentials={"token": "abc"},
            auth_kind="bearer",
        )
    )
    envelope = json.dumps(
        {
            "query": (
                "query Top { repositories(first: 2) { edges "
                "{ node { id stars } } } }"
            )
        }
    )
    rs = await eng.execute(envelope)
    assert rs.columns == ["id", "stars"]
    assert rs.rows == [["1", 50], ["2", 99]]
    assert rs.row_count == 2
    # bearer header travelled through
    assert captured["auth"] == "Bearer abc"
    # body is the POST envelope (no operationName, just query+variables)
    assert "query" in captured["body"]
    assert captured["body"]["variables"] == {}


@pytest.mark.asyncio
async def test_execute_rejects_mutation_at_engine_layer(
    monkeypatch,
) -> None:
    eng = GraphqlEngine(
        _source(
            connection_meta={"endpoint": "https://api.example/graphql"}
        )
    )
    envelope = json.dumps(
        {"query": "mutation { dropEverything { ok } }"}
    )
    with pytest.raises(ValueError, match="read-only"):
        await eng.execute(envelope)


@pytest.mark.asyncio
async def test_execute_propagates_graphql_errors(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{"message": "Field 'nope' not found"}],
            },
        )

    transport = httpx.MockTransport(handler)
    import app.engines.graphql as gqm

    orig = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(gqm.httpx, "AsyncClient", _patched)
    eng = GraphqlEngine(
        _source(
            connection_meta={"endpoint": "https://api.example/graphql"}
        )
    )
    envelope = json.dumps({"query": "{ nope }"})
    with pytest.raises(RuntimeError, match="not found"):
        await eng.execute(envelope)


@pytest.mark.asyncio
async def test_api_key_header_auth(monkeypatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["xkey"] = req.headers.get("X-API-Key")
        return httpx.Response(200, json={"data": {"x": []}})

    import app.engines.graphql as gqm

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(gqm.httpx, "AsyncClient", _patched)
    eng = GraphqlEngine(
        _source(
            connection_meta={"endpoint": "https://api.example/graphql"},
            credentials={
                "key": "topsecret",
                "key_location": "header",
                "key_name": "X-API-Key",
            },
            auth_kind="api_key",
        )
    )
    await eng.execute(json.dumps({"query": "{x}"}))
    assert captured["xkey"] == "topsecret"


@pytest.mark.asyncio
async def test_api_key_query_param_auth(monkeypatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"data": {"x": []}})

    import app.engines.graphql as gqm

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(gqm.httpx, "AsyncClient", _patched)
    eng = GraphqlEngine(
        _source(
            connection_meta={"endpoint": "https://api.example/graphql"},
            credentials={
                "key": "k",
                "key_location": "query",
                "key_name": "api_key",
            },
            auth_kind="api_key",
        )
    )
    await eng.execute(json.dumps({"query": "{x}"}))
    assert "api_key=k" in captured["url"]


@pytest.mark.asyncio
async def test_basic_auth(monkeypatch) -> None:
    import base64

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={"data": {}})

    import app.engines.graphql as gqm

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(gqm.httpx, "AsyncClient", _patched)
    eng = GraphqlEngine(
        _source(
            connection_meta={"endpoint": "https://api.example/graphql"},
            credentials={"username": "alice", "password": "wonder"},
            auth_kind="basic",
        )
    )
    await eng.execute(json.dumps({"query": "{x}"}))
    expected = "Basic " + base64.b64encode(
        b"alice:wonder"
    ).decode()
    assert captured["auth"] == expected
