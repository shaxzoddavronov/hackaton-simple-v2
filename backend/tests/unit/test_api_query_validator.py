from __future__ import annotations

import json

import pytest

from app.engines.base import ColumnMeta, SchemaBundle, TableMeta
from app.services.api_query_validator import validate_api_query


def _bundle(*paths: str) -> SchemaBundle:
    """Build a tiny SchemaBundle that mimics what RestApiEngine returns."""
    return SchemaBundle(
        dialect="rest_api",
        tables=[
            TableMeta(
                schema="api",
                name=f"GET {p}",
                columns=[],
                foreign_keys=[],
            )
            for p in paths
        ],
    )


def _env(**overrides) -> str:
    """Construct a baseline GET envelope, override fields, JSON-encode."""
    base = {
        "endpoint": "/users",
        "method": "GET",
    }
    base.update(overrides)
    return json.dumps(base)


def test_invalid_json_rejected() -> None:
    result, env = validate_api_query("{not json")
    assert not result.ok
    assert env is None
    assert any(f.code == "api_invalid_json" for f in result.findings)


def test_array_root_rejected() -> None:
    result, env = validate_api_query("[]")
    assert not result.ok
    assert env is None
    assert any(f.code == "api_invalid_json" for f in result.findings)


def test_missing_endpoint() -> None:
    result, env = validate_api_query(json.dumps({"method": "GET"}))
    assert not result.ok
    assert env is None
    assert any(f.code == "api_missing_endpoint" for f in result.findings)


def test_missing_method() -> None:
    result, env = validate_api_query(json.dumps({"endpoint": "/u"}))
    assert not result.ok
    assert any(f.code == "api_missing_method" for f in result.findings)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_non_get_methods_rejected(method: str) -> None:
    result, env = validate_api_query(_env(method=method))
    assert not result.ok
    assert any(f.code == "api_method_not_get" for f in result.findings)


def test_get_accepted() -> None:
    result, env = validate_api_query(_env())
    assert result.ok
    assert env is not None
    # Canonical rewrite: sorted-key JSON.
    rewritten = json.loads(result.rewritten_sql)
    assert rewritten["endpoint"] == "/users"
    assert rewritten["method"] == "GET"


def test_absolute_url_rejected_ssrf_guard() -> None:
    result, _ = validate_api_query(
        json.dumps({"endpoint": "http://169.254.169.254/latest/", "method": "GET"})
    )
    assert not result.ok
    assert any(f.code == "api_absolute_url" for f in result.findings)


def test_path_traversal_rejected() -> None:
    result, _ = validate_api_query(_env(endpoint="/users/../admin"))
    assert not result.ok
    assert any(f.code == "api_path_traversal" for f in result.findings)


def test_endpoint_not_starting_with_slash_rejected() -> None:
    result, _ = validate_api_query(_env(endpoint="users"))
    assert not result.ok
    assert any(f.code == "api_invalid_endpoint" for f in result.findings)


def test_endpoint_in_catalog_accepted() -> None:
    bundle = _bundle("/users", "/contacts")
    result, _ = validate_api_query(_env(endpoint="/users"), schema_bundle=bundle)
    assert result.ok


def test_endpoint_not_in_catalog_rejected() -> None:
    bundle = _bundle("/users", "/contacts")
    result, _ = validate_api_query(_env(endpoint="/orders"), schema_bundle=bundle)
    assert not result.ok
    assert any(f.code == "api_endpoint_not_in_catalog" for f in result.findings)


def test_path_template_matches_concrete_id() -> None:
    bundle = _bundle("/users/{id}", "/users")
    result, _ = validate_api_query(_env(endpoint="/users/123"), schema_bundle=bundle)
    assert result.ok


def test_path_template_multi_segment() -> None:
    bundle = _bundle("/users/{id}/orders")
    result, _ = validate_api_query(
        _env(endpoint="/users/abc/orders"), schema_bundle=bundle
    )
    assert result.ok


def test_path_template_lengths_differ_rejected() -> None:
    bundle = _bundle("/users/{id}")
    result, _ = validate_api_query(
        _env(endpoint="/users/123/orders"), schema_bundle=bundle
    )
    assert not result.ok


def test_query_params_with_nested_object_rejected() -> None:
    result, _ = validate_api_query(
        _env(query_params={"filter": {"name": "alice"}})
    )
    assert not result.ok
    assert any(f.code == "api_invalid_param_type" for f in result.findings)


def test_query_params_with_scalar_list_accepted() -> None:
    result, _ = validate_api_query(
        _env(query_params={"tag": ["a", "b"], "limit": 50})
    )
    assert result.ok


def test_json_path_must_be_string() -> None:
    result, _ = validate_api_query(_env(json_path=123))
    assert not result.ok
    assert any(f.code == "api_invalid_json_path" for f in result.findings)


def test_row_field_paths_must_map_str_str() -> None:
    result, _ = validate_api_query(_env(row_field_paths={"id": 1}))
    assert not result.ok
    assert any(f.code == "api_invalid_row_field_paths" for f in result.findings)


def test_unknown_top_keys_warn_but_accepted() -> None:
    result, _ = validate_api_query(_env(extra_field="ignored"))
    assert result.ok
    # warning finding present but not blocking
    assert any(f.code == "api_unknown_keys" for f in result.findings)


def test_canonical_rewrite_sorts_keys() -> None:
    raw = json.dumps({"method": "GET", "endpoint": "/u", "headers": {}})
    result, _ = validate_api_query(raw)
    assert result.ok
    rewritten = result.rewritten_sql
    # Keys appear in alphabetical order in the rewrite.
    assert rewritten.index('"endpoint"') < rewritten.index('"headers"')
    assert rewritten.index('"headers"') < rewritten.index('"method"')
