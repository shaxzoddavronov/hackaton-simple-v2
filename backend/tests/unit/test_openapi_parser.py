from __future__ import annotations

import base64
import json

import pytest

from app.services.openapi_parser import (
    ParsedEndpoint,
    load_spec_base64,
    load_spec_text,
    parse_openapi,
)


MINIMAL_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Demo", "version": "1.0"},
    "paths": {
        "/users": {
            "get": {
                "summary": "List users",
                "parameters": [
                    {"name": "limit", "in": "query",
                     "schema": {"type": "integer"}, "required": False},
                    {"name": "name", "in": "query",
                     "schema": {"type": "string"}, "required": False},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/User"},
                                }
                            }
                        },
                    }
                },
            }
        },
        "/users/{id}": {
            "get": {
                "summary": "Get user",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/User"}
                            }
                        },
                    }
                },
            },
            "post": {  # ignored (non-GET)
                "summary": "Create user",
                "responses": {"201": {"description": "created"}},
            },
        },
    },
    "components": {
        "schemas": {
            "User": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "email": {"type": "string", "format": "email"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            }
        }
    },
}


def test_load_spec_text_parses_json() -> None:
    spec = load_spec_text(json.dumps(MINIMAL_SPEC))
    assert spec["openapi"] == "3.0.0"


def test_load_spec_text_rejects_non_object_root() -> None:
    with pytest.raises(ValueError):
        load_spec_text("[1,2,3]")


def test_load_spec_base64_roundtrips() -> None:
    b64 = base64.b64encode(json.dumps(MINIMAL_SPEC).encode("utf-8")).decode("ascii")
    spec = load_spec_base64(b64)
    assert spec["info"]["title"] == "Demo"


def test_parse_openapi_yields_two_get_endpoints() -> None:
    eps = parse_openapi(MINIMAL_SPEC)
    # Two paths × one GET each. POST is filtered out.
    assert len(eps) == 2
    paths = {e.path for e in eps}
    assert paths == {"/users", "/users/{id}"}
    assert all(e.method == "GET" for e in eps)


def test_parse_openapi_extracts_query_params_with_types() -> None:
    eps = parse_openapi(MINIMAL_SPEC)
    users = next(e for e in eps if e.path == "/users")
    by_name = {p.name: p for p in users.params}
    assert by_name["limit"].type == "integer"
    assert by_name["limit"].location == "query"
    assert by_name["name"].type == "string"


def test_parse_openapi_marks_path_params_required() -> None:
    eps = parse_openapi(MINIMAL_SPEC)
    detail = next(e for e in eps if e.path == "/users/{id}")
    by_name = {p.name: p for p in detail.params}
    assert by_name["id"].location == "path"
    assert by_name["id"].required is True


def test_parse_openapi_resolves_ref_and_drills_arrays() -> None:
    eps = parse_openapi(MINIMAL_SPEC)
    users = next(e for e in eps if e.path == "/users")
    field_names = {f.name for f in users.response_fields}
    # The array's items ref → User schema → its top-level properties.
    assert field_names == {"id", "name", "email", "created_at"}
    by_name = {f.name: f for f in users.response_fields}
    assert by_name["id"].type == "integer"
    assert by_name["created_at"].type == "timestamp"
    # required=[id,name] → those two are non-nullable.
    assert by_name["id"].nullable is False
    assert by_name["name"].nullable is False
    assert by_name["email"].nullable is True


def test_parse_openapi_handles_swagger2_inline_schema() -> None:
    swagger2 = {
        "swagger": "2.0",
        "paths": {
            "/v1/things": {
                "get": {
                    "parameters": [
                        {"name": "q", "in": "query", "type": "string"},
                    ],
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "count": {"type": "integer"},
                                },
                            }
                        }
                    },
                }
            }
        },
    }
    eps = parse_openapi(swagger2)
    assert len(eps) == 1
    ep = eps[0]
    assert ep.path == "/v1/things"
    assert ep.params[0].name == "q"
    assert ep.response_fields[0].name == "count"


def test_parse_openapi_empty_when_paths_missing() -> None:
    assert parse_openapi({}) == []
    assert parse_openapi({"paths": "not-a-dict"}) == []
