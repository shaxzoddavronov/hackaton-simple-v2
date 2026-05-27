from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app.engines.rest_api import (
    RestApiEngine,
    RestApiError,
    _extract_rows,
    _flatten_rows,
    _resolve_dot_path,
)


def _source(
    *,
    connection_meta: dict,
    credentials: dict | None = None,
    auth_kind: str = "none",
):
    """Construct the duck-typed `source` argument the engine expects."""
    return SimpleNamespace(
        dialect="rest_api",
        connection_meta=connection_meta,
        _credentials=credentials or {},
        auth_kind=auth_kind,
    )


# ── dot-path resolver ────────────────────────────────────────────


def test_resolve_dot_path_root() -> None:
    assert _resolve_dot_path({"a": 1}, "$") == {"a": 1}


def test_resolve_dot_path_simple() -> None:
    payload = {"data": {"items": [1, 2, 3]}}
    assert _resolve_dot_path(payload, "$.data.items") == [1, 2, 3]


def test_resolve_dot_path_with_index() -> None:
    payload = {"data": {"items": [{"id": 1}, {"id": 2}]}}
    assert _resolve_dot_path(payload, "$.data.items[1].id") == 2


def test_resolve_dot_path_missing_returns_none() -> None:
    assert _resolve_dot_path({"a": 1}, "$.b.c") is None


# ── row extraction ───────────────────────────────────────────────


def test_extract_rows_explicit_json_path() -> None:
    payload = {"data": {"items": [{"id": 1}, {"id": 2}]}}
    rows = _extract_rows(payload, "$.data.items")
    assert rows == [{"id": 1}, {"id": 2}]


def test_extract_rows_fallback_to_value() -> None:
    """OData returns rows under $.value — fallback heuristic finds them."""
    payload = {"@odata.count": 2, "value": [{"id": 1}, {"id": 2}]}
    rows = _extract_rows(payload, None)
    assert rows == [{"id": 1}, {"id": 2}]


def test_extract_rows_fallback_to_results() -> None:
    """HubSpot pattern."""
    payload = {"results": [{"id": "a"}], "paging": {}}
    rows = _extract_rows(payload, None)
    assert rows == [{"id": "a"}]


def test_extract_rows_root_array() -> None:
    assert _extract_rows([{"a": 1}, {"a": 2}], None) == [{"a": 1}, {"a": 2}]


def test_extract_rows_single_list_dict() -> None:
    """Single-list-value dict like {'contacts':[...]} is treated as the row array."""
    payload = {"contacts": [{"id": 1}, {"id": 2}]}
    rows = _extract_rows(payload, None)
    assert rows == [{"id": 1}, {"id": 2}]


# ── row flattening ───────────────────────────────────────────────


def test_flatten_rows_with_field_paths() -> None:
    rows = [
        {"id": 1, "props": {"name": "alice"}},
        {"id": 2, "props": {"name": "bob"}},
    ]
    cols, dtypes, out = _flatten_rows(
        rows, {"id": "$.id", "name": "$.props.name"}, row_cap=10
    )
    assert cols == ["id", "name"]
    assert out == [[1, "alice"], [2, "bob"]]
    assert dtypes == ["bigint", "text"]


def test_flatten_rows_auto_picks_scalar_keys() -> None:
    rows = [
        {"id": 1, "name": "alice", "props": {"x": 1}},
        {"id": 2, "name": "bob", "props": {"x": 2}},
    ]
    cols, dtypes, out = _flatten_rows(rows, {}, row_cap=10)
    assert "id" in cols and "name" in cols
    # nested dict is skipped
    assert "props" not in cols
    # bigint for id, text for name
    by_col = dict(zip(cols, dtypes))
    assert by_col["id"] == "bigint"
    assert by_col["name"] == "text"


def test_flatten_rows_truncates_to_row_cap() -> None:
    rows = [{"id": i} for i in range(50)]
    cols, _, out = _flatten_rows(rows, {}, row_cap=5)
    assert len(out) == 5


# ── engine: registry + introspection from preset ─────────────────


def test_engine_registered_under_rest_api() -> None:
    from app.engines import register_all
    from app.engines.registry import DIALECT_REGISTRY

    register_all()
    assert "rest_api" in DIALECT_REGISTRY


def test_engine_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        RestApiEngine(_source(connection_meta={}))


@pytest.mark.asyncio
async def test_introspect_from_preset_bitrix24() -> None:
    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://demo.bitrix24.com",
                "spec_source": "preset",
                "preset": "bitrix24",
            }
        )
    )
    bundle = await engine.introspect_schema()
    assert bundle.dialect == "rest_api"
    # The preset ships at least 5 endpoints.
    assert len(bundle.tables) >= 5
    names = {t.name for t in bundle.tables}
    assert "GET /rest/crm.contact.list" in names


@pytest.mark.asyncio
async def test_introspect_from_openapi_file() -> None:
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/v1/widgets": {
                "get": {
                    "parameters": [
                        {"name": "limit", "in": "query",
                         "schema": {"type": "integer"}}
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "name": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }
    b64 = base64.b64encode(json.dumps(spec).encode()).decode("ascii")
    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://api.example.com",
                "spec_source": "openapi_file",
                "spec_content_b64": b64,
            }
        )
    )
    bundle = await engine.introspect_schema()
    assert len(bundle.tables) == 1
    table = bundle.tables[0]
    assert table.name == "GET /v1/widgets"
    # Response fields + 1 query param (prefixed with @).
    col_names = {c.name for c in table.columns}
    assert {"id", "name", "@limit"} <= col_names


@pytest.mark.asyncio
async def test_introspect_unknown_spec_source() -> None:
    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://x",
                "spec_source": "preset",
                # missing 'preset' name
            }
        )
    )
    with pytest.raises(ValueError, match="preset"):
        await engine.introspect_schema()


# ── engine: execute with mocked transport ────────────────────────


@pytest.mark.asyncio
async def test_execute_bearer_auth_sets_authorization_header() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json={"data": [{"id": 1, "name": "x"}]})

    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://api.example.com",
                "spec_source": "none",
            },
            credentials={"token": "secret-abc"},
            auth_kind="bearer",
        )
    )
    # Inject a MockTransport-backed client so no network call happens.
    engine._client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
    )

    envelope = json.dumps(
        {"endpoint": "/v1/users", "method": "GET", "json_path": "$.data"}
    )
    rs = await engine.execute(envelope, row_cap=10, timeout_s=5)
    assert seen_headers["authorization"] == "Bearer secret-abc"
    assert rs.row_count == 1
    assert "name" in rs.columns


@pytest.mark.asyncio
async def test_execute_api_key_query_param() -> None:
    seen_url: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_url.append(str(request.url))
        return httpx.Response(200, json={"items": [{"id": "a"}]})

    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://api.example.com",
                "spec_source": "none",
            },
            credentials={"key": "sk-xyz", "key_location": "query", "key_name": "api_key"},
            auth_kind="api_key",
        )
    )
    engine._client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
    )

    envelope = json.dumps({"endpoint": "/v1/x", "method": "GET"})
    await engine.execute(envelope, row_cap=5, timeout_s=5)
    assert "api_key=sk-xyz" in seen_url[0]


@pytest.mark.asyncio
async def test_execute_api_key_header_default() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json={"data": []})

    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://api.example.com",
                "spec_source": "none",
            },
            credentials={"key": "K", "key_name": "X-Custom-Key"},
            auth_kind="api_key",
        )
    )
    engine._client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
    )

    envelope = json.dumps({"endpoint": "/x", "method": "GET"})
    await engine.execute(envelope, row_cap=5, timeout_s=5)
    assert seen_headers["x-custom-key"] == "K"


@pytest.mark.asyncio
async def test_execute_path_param_substitution() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"id": 42})

    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://api.example.com",
                "spec_source": "none",
            }
        )
    )
    engine._client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
    )

    envelope = json.dumps(
        {
            "endpoint": "/users/{id}",
            "method": "GET",
            "path_params": {"id": "42"},
        }
    )
    await engine.execute(envelope, row_cap=5, timeout_s=5)
    assert seen_paths[0] == "/users/42"


@pytest.mark.asyncio
async def test_execute_raises_rest_api_error_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://api.example.com",
                "spec_source": "none",
            }
        )
    )
    engine._client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
    )

    envelope = json.dumps({"endpoint": "/missing", "method": "GET"})
    with pytest.raises(RestApiError) as exc:
        await engine.execute(envelope, row_cap=5, timeout_s=5)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_execute_rejects_post_envelope() -> None:
    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://x",
                "spec_source": "none",
            }
        )
    )
    envelope = json.dumps({"endpoint": "/u", "method": "POST"})
    with pytest.raises(ValueError, match="method"):
        await engine.execute(envelope, row_cap=5, timeout_s=5)


@pytest.mark.asyncio
async def test_execute_with_row_field_paths_shapes_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {"id": 1, "properties": {"name": "alice", "tier": "gold"}},
                        {"id": 2, "properties": {"name": "bob", "tier": "silver"}},
                    ]
                }
            },
        )

    engine = RestApiEngine(
        _source(
            connection_meta={
                "base_url": "https://api.example.com",
                "spec_source": "none",
            }
        )
    )
    engine._client = httpx.AsyncClient(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
    )

    envelope = json.dumps(
        {
            "endpoint": "/v1/customers",
            "method": "GET",
            "json_path": "$.data.items",
            "row_field_paths": {
                "id": "$.id",
                "name": "$.properties.name",
                "tier": "$.properties.tier",
            },
        }
    )
    rs = await engine.execute(envelope, row_cap=10, timeout_s=5)
    assert rs.columns == ["id", "name", "tier"]
    assert rs.row_count == 2
    assert rs.rows[0] == [1, "alice", "gold"]


# ── presets module sanity ────────────────────────────────────────


def test_each_preset_returns_endpoints() -> None:
    from app.services.api_presets import PRESETS, load_preset

    for name in ("bitrix24", "amocrm", "odata_1c", "hubspot", "salesforce"):
        eps = load_preset(name)
        assert len(eps) >= 4, f"{name} preset must have at least 4 endpoints"
        assert all(e.method == "GET" for e in eps)
    assert load_preset("generic") == []
    assert "bitrix24" in PRESETS


def test_unknown_preset_raises() -> None:
    from app.services.api_presets import load_preset

    with pytest.raises(ValueError):
        load_preset("nonexistent-crm")
