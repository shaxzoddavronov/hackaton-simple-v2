"""Phase 31 — MCP server JSON-RPC dispatch + tool descriptors.

We test the pure-Python dispatch logic without touching the actual
database or agent graph. Tool implementations that hit the DB /
agent are exercised through the e2e Postgres fixture (which the
unit suite skips when no DB is running).
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.mcp.server import (
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_UNAUTHORIZED,
    SERVER_INFO,
    _coerce_uuid,
    _ParamError,
    handle_request,
    tool_descriptors,
)


# ── Descriptors ──────────────────────────────────────────────────


def test_server_info_has_name_and_version() -> None:
    assert SERVER_INFO["name"] == "querymind"
    assert "version" in SERVER_INFO


def test_tool_descriptors_cover_three_tools() -> None:
    out = tool_descriptors()
    names = {t["name"] for t in out}
    assert names == {
        "querymind.list_workspaces",
        "querymind.workspace_schema",
        "querymind.ask",
    }


def test_every_tool_has_input_schema() -> None:
    for t in tool_descriptors():
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"


def test_ask_tool_requires_workspace_id_and_question() -> None:
    ask = next(
        t for t in tool_descriptors() if t["name"] == "querymind.ask"
    )
    required = set(ask["inputSchema"].get("required") or [])
    assert required == {"workspace_id", "question"}


def test_workspace_schema_tool_requires_workspace_id() -> None:
    t = next(
        x for x in tool_descriptors()
        if x["name"] == "querymind.workspace_schema"
    )
    required = set(t["inputSchema"].get("required") or [])
    assert required == {"workspace_id"}


# ── _coerce_uuid ─────────────────────────────────────────────────


def test_coerce_uuid_accepts_valid() -> None:
    uid = uuid4()
    assert _coerce_uuid(str(uid)) == uid


def test_coerce_uuid_rejects_garbage() -> None:
    with pytest.raises(_ParamError):
        _coerce_uuid("not-a-uuid")


# ── JSON-RPC dispatch (no DB required) ───────────────────────────


@pytest.mark.asyncio
async def test_initialize_returns_capabilities() -> None:
    resp = await handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert resp is not None
    assert resp["id"] == 1
    res = resp["result"]
    assert res["protocolVersion"] == "2024-11-05"
    assert "tools" in res["capabilities"]
    assert res["serverInfo"]["name"] == "querymind"


@pytest.mark.asyncio
async def test_tools_list_returns_descriptors() -> None:
    resp = await handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert resp is not None
    assert "tools" in resp["result"]
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "querymind.ask" in names


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found() -> None:
    resp = await handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "nope"}
    )
    assert resp is not None
    assert resp["error"]["code"] == ERR_METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_missing_method_field_returns_invalid_request() -> None:
    resp = await handle_request({"jsonrpc": "2.0", "id": 4})
    assert resp is not None
    assert resp["error"]["code"] == ERR_INVALID_REQUEST


@pytest.mark.asyncio
async def test_notification_returns_none() -> None:
    """No ``id`` field → MCP notification → no response."""
    out = await handle_request(
        {"jsonrpc": "2.0", "method": "initialize"}
    )
    assert out is None


@pytest.mark.asyncio
async def test_non_dict_request_returns_invalid_request() -> None:
    resp = await handle_request("not a dict")  # type: ignore[arg-type]
    assert resp is not None
    assert resp["error"]["code"] == ERR_INVALID_REQUEST


@pytest.mark.asyncio
async def test_tools_call_without_token_returns_unauthorized() -> None:
    resp = await handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "querymind.list_workspaces",
                "arguments": {},
            },
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == ERR_UNAUTHORIZED


@pytest.mark.asyncio
async def test_tools_call_with_bogus_token_returns_unauthorized() -> None:
    resp = await handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "querymind.list_workspaces",
                "arguments": {},
                "token": "not-a-jwt",
            },
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == ERR_UNAUTHORIZED


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_returns_invalid_params() -> None:
    # Use a token that decodes cleanly so we get past auth into the
    # tool-dispatch path.
    from jose import jwt as jose_jwt

    from app.config import settings

    token = jose_jwt.encode(
        {"sub": str(uuid4())},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALG,
    )
    resp = await handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "querymind.does_not_exist",
                "arguments": {},
                "token": token,
            },
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == ERR_INVALID_PARAMS
    assert "unknown tool" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_tools_call_ask_with_missing_question_rejects() -> None:
    from jose import jwt as jose_jwt

    from app.config import settings

    token = jose_jwt.encode(
        {"sub": str(uuid4())},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALG,
    )
    resp = await handle_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "querymind.ask",
                "arguments": {"workspace_id": str(uuid4())},
                "token": token,
            },
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == ERR_INVALID_PARAMS
    assert "question" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_tools_call_ask_with_bad_workspace_uuid_rejects() -> None:
    from jose import jwt as jose_jwt

    from app.config import settings

    token = jose_jwt.encode(
        {"sub": str(uuid4())},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALG,
    )
    resp = await handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "querymind.ask",
                "arguments": {
                    "workspace_id": "not-a-uuid",
                    "question": "x",
                },
                "token": token,
            },
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == ERR_INVALID_PARAMS
