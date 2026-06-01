"""Transport-agnostic MCP server logic.

The MCP wire protocol is JSON-RPC 2.0 over either stdio or HTTP/
WebSocket. We hand-roll just the methods QueryMind needs — adding a
full SDK dep (``mcp`` package) would pull in async-stdio plumbing
this module doesn't use.

Three RPC methods are surfaced. Their names match the conventional
MCP shape (``namespace.method``) so client display ("call_tool")
stays readable:

  * ``initialize``   — handshake. Returns server name + version +
    list of tools.
  * ``tools/list``   — same tool list as ``initialize`` for
    clients that don't cache.
  * ``tools/call``   — invoke a single tool by name with arguments.

Tools (registered in this module):

  * ``querymind.list_workspaces``
      {} → [{id, name, status, connection_count}, ...]
  * ``querymind.workspace_schema``
      {workspace_id} → [{connection_name, dialect, tables: [...]}, ...]
  * ``querymind.ask``
      {workspace_id, question, connection_id?} → {ui_spec, sql,
       citations, sub_results}

Authentication: every RPC call (except ``initialize``) requires
``params.token`` containing a QueryMind JWT. The stdio transport
fills it from the ``QM_TOKEN`` env var; HTTP fills it from the
Authorization header.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)


# JSON-RPC error codes — the standard set + our extensions.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_UNAUTHORIZED = -32001   # custom, MCP spec leaves -32000…-32099 open


SERVER_INFO = {
    "name": "querymind",
    "version": "1.0.0",
    "description": (
        "QueryMind agentic NL-to-SQL/RAG over multi-dialect "
        "workspaces. Exposes workspace listing, schema introspection, "
        "and end-to-end natural-language queries as MCP tools."
    ),
}


def tool_descriptors() -> list[dict[str, Any]]:
    """Static JSON-schema descriptors for every exposed tool.

    Clients (Claude Desktop, etc.) render these into UI affordances.
    Keep the descriptions tight: the user sees them as one-liners
    next to the tool name in the picker.
    """
    return [
        {
            "name": "querymind.list_workspaces",
            "description": (
                "List the caller's QueryMind workspaces. Each "
                "workspace is a folder holding multiple database / "
                "API / file connections."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "querymind.workspace_schema",
            "description": (
                "Return every connection's schema bundle for a "
                "workspace — tables, columns, dialects. Use this "
                "before asking complex questions so the model knows "
                "what's queryable."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "type": "string",
                        "description": "UUID of the workspace.",
                    }
                },
                "required": ["workspace_id"],
            },
        },
        {
            "name": "querymind.ask",
            "description": (
                "Run a natural-language question through QueryMind's "
                "agent and return the final UI spec + SQL + "
                "citations. The agent picks the right connection, "
                "validates the query is read-only, executes it, and "
                "summarises the result."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "type": "string",
                        "description": "UUID of the workspace to query.",
                    },
                    "question": {
                        "type": "string",
                        "description": (
                            "Natural-language question. Uzbek, Russian, "
                            "and English are all supported; the answer "
                            "comes back in the question's language."
                        ),
                    },
                    "connection_id": {
                        "type": "string",
                        "description": (
                            "Optional. UUID of a specific connection in "
                            "the workspace. If omitted the agent "
                            "auto-resolves based on the question and the "
                            "workspace's connections."
                        ),
                    },
                },
                "required": ["workspace_id", "question"],
            },
        },
    ]


# ── JSON-RPC dispatch ─────────────────────────────────────────────


async def handle_request(req: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC request. Returns the response dict, or
    None for notifications (no ``id`` field).

    Wraps every method handler in a try/except that converts
    exceptions into MCP error responses. The caller (stdio loop or
    HTTP route) only ever sees a fully-formed JSON-RPC payload.
    """
    if not isinstance(req, dict):
        return _err(None, ERR_INVALID_REQUEST, "request must be an object")
    rpc_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if not isinstance(method, str):
        return _err(rpc_id, ERR_INVALID_REQUEST, "missing 'method' string")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        elif method == "tools/list":
            result = {"tools": tool_descriptors()}
        elif method == "tools/call":
            result = await _call_tool(params)
        else:
            return _err(
                rpc_id, ERR_METHOD_NOT_FOUND, f"unknown method {method!r}"
            )
    except _AuthError as e:
        return _err(rpc_id, ERR_UNAUTHORIZED, str(e))
    except _ParamError as e:
        return _err(rpc_id, ERR_INVALID_PARAMS, str(e))
    except Exception as e:
        log.exception("MCP handler crashed on method=%s", method)
        return _err(rpc_id, ERR_INTERNAL, str(e)[:300])

    if rpc_id is None:
        return None  # notification — no response
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message},
    }


# ── Tool dispatch ────────────────────────────────────────────────


class _AuthError(RuntimeError):
    """Raised by tool handlers when the JWT is missing / invalid.

    Caller maps to JSON-RPC -32001."""


class _ParamError(ValueError):
    """Raised when a tool call's arguments don't match the schema."""


async def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    token = params.get("token")  # caller-supplied JWT
    if not isinstance(name, str):
        raise _ParamError("tools/call requires 'name' string")
    user_id = await _resolve_user(token)

    if name == "querymind.list_workspaces":
        rows = await _list_workspaces(user_id)
    elif name == "querymind.workspace_schema":
        ws_id = _require_uuid(args, "workspace_id")
        rows = await _workspace_schema(user_id, ws_id)
    elif name == "querymind.ask":
        ws_id = _require_uuid(args, "workspace_id")
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            raise _ParamError("'question' is required")
        conn_id_raw = args.get("connection_id")
        conn_id = (
            _coerce_uuid(conn_id_raw) if conn_id_raw else None
        )
        rows = await _ask(user_id, ws_id, question, conn_id)
    else:
        raise _ParamError(f"unknown tool {name!r}")

    # MCP "tools/call" wraps a single content item per result.
    # We use a single ``text`` block carrying the JSON payload so
    # any MCP client can render or post-process.
    import json as _json

    return {
        "content": [
            {"type": "text", "text": _json.dumps(rows, ensure_ascii=False)}
        ],
        "isError": False,
    }


def _require_uuid(args: dict[str, Any], key: str) -> UUID:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw:
        raise _ParamError(f"'{key}' is required")
    return _coerce_uuid(raw)


def _coerce_uuid(raw: str) -> UUID:
    try:
        return UUID(raw)
    except (ValueError, TypeError) as e:
        raise _ParamError(f"invalid UUID: {raw!r}") from e


# ── Auth + tool implementations ──────────────────────────────────


async def _resolve_user(token: str | None) -> UUID:
    """Validate the JWT and return the user UUID.

    Reuses the same JWT decoder the FastAPI auth dependency uses so
    a token issued via ``POST /auth/login`` works identically against
    the MCP server.
    """
    if not isinstance(token, str) or not token:
        raise _AuthError(
            "missing token — set QM_TOKEN env var (stdio) or pass "
            "Authorization: Bearer <jwt> (HTTP)"
        )
    try:
        from jose import jwt as jose_jwt

        from app.config import settings

        payload = jose_jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG]
        )
    except Exception as e:
        raise _AuthError(f"invalid token: {e}") from e
    sub = payload.get("sub")
    if not sub:
        raise _AuthError("token missing 'sub' claim")
    try:
        return UUID(sub)
    except (ValueError, TypeError) as e:
        raise _AuthError(f"token 'sub' is not a UUID: {sub}") from e


async def _list_workspaces(user_id: UUID) -> list[dict]:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.db.models import Workspace, WorkspaceConnection

    eng = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    out: list[dict] = []
    try:
        async with Session() as session:
            rows = (
                await session.execute(
                    select(Workspace).where(Workspace.owner_id == user_id)
                )
            ).scalars().all()
            for w in rows:
                cnt = (
                    await session.execute(
                        select(func.count(WorkspaceConnection.id)).where(
                            WorkspaceConnection.workspace_id == w.id
                        )
                    )
                ).scalar_one()
                out.append(
                    {
                        "id": str(w.id),
                        "name": w.name,
                        "status": w.status,
                        "connection_count": int(cnt),
                    }
                )
    finally:
        await eng.dispose()
    return out


async def _workspace_schema(
    user_id: UUID, workspace_id: UUID
) -> list[dict]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.db.models import (
        SchemaBundle as SchemaBundleRow,
        Workspace,
        WorkspaceConnection,
    )

    eng = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    out: list[dict] = []
    try:
        async with Session() as session:
            ws = await session.get(Workspace, workspace_id)
            if ws is None or ws.owner_id != user_id:
                raise _AuthError("workspace not found or not owned by caller")
            conns = (
                await session.execute(
                    select(WorkspaceConnection).where(
                        WorkspaceConnection.workspace_id == workspace_id
                    )
                )
            ).scalars().all()
            for c in conns:
                bundle = await session.get(SchemaBundleRow, c.id)
                bundle_data = (
                    bundle.bundle if bundle is not None else {"tables": []}
                )
                if isinstance(bundle_data, str):
                    import json as _json

                    try:
                        bundle_data = _json.loads(bundle_data)
                    except Exception:
                        bundle_data = {"tables": []}
                out.append(
                    {
                        "connection_id": str(c.id),
                        "connection_name": c.name,
                        "dialect": c.dialect,
                        "status": c.status,
                        "tables": bundle_data.get("tables", []),
                    }
                )
    finally:
        await eng.dispose()
    return out


async def _ask(
    user_id: UUID,
    workspace_id: UUID,
    question: str,
    connection_id: UUID | None,
) -> dict:
    """Run the question through the same agent graph the chat
    endpoint uses, then return a compact summary suitable for the
    MCP client to display.

    No streaming — MCP clients want a single response payload.
    The chat SSE intermediate frames are dropped here.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.db.models import Workspace
    from app.engines import register_all as register_engines

    eng = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Session() as session:
            ws = await session.get(Workspace, workspace_id)
            if ws is None or ws.owner_id != user_id:
                raise _AuthError(
                    "workspace not found or not owned by caller"
                )
    finally:
        await eng.dispose()

    register_engines()
    from app.agents.graph import build_graph
    from app.agents.state import GraphState

    graph = build_graph()
    init: GraphState = {
        "user_id": user_id,
        "user_message": question,
        "active_workspace_id": workspace_id,
        "active_connection_id": connection_id,
        "conversation_history": [],
    }  # type: ignore[typeddict-item]
    final = await graph.ainvoke(init)

    ui_spec = final.get("ui_spec")
    answer = final.get("answer")
    citations = final.get("citations") or []
    sub_results = final.get("sub_results") or {}
    sql = final.get("sql_executed")

    return {
        "headline": getattr(answer, "headline", None) if answer else None,
        "body_md": getattr(answer, "body_md", None) if answer else None,
        "ui_spec": (
            ui_spec.model_dump(mode="json")
            if ui_spec is not None and hasattr(ui_spec, "model_dump")
            else ui_spec
        ),
        "sql": sql,
        "citations": citations,
        "sub_results": sub_results,
        "error": final.get("error_message"),
    }


__all__ = [
    "SERVER_INFO",
    "tool_descriptors",
    "handle_request",
]
