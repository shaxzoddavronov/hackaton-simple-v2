"""HTTP transport for the MCP server.

Phase 31. Mounts a single ``POST /mcp`` route that accepts a JSON-
RPC payload and returns the JSON-RPC response. The Authorization
header's bearer JWT is forwarded into ``params.token`` so the
underlying tool dispatcher sees the same shape stdio uses.

We don't bind to the standard FastAPI auth dependency here for one
reason: MCP's spec allows clients to call ``initialize`` and
``tools/list`` without auth, and only require credentials for
``tools/call``. The dependency would force JWT validation on every
hit. Instead the auth check lives inside :mod:`app.mcp.server` per
RPC method.

Future v2: add SSE / streamable-HTTP transport for clients that
want incremental tool results. v1 is request/response only.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.mcp.server import handle_request

log = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.post("")
async def mcp_endpoint(request: Request) -> dict:
    """One JSON-RPC roundtrip per HTTP request. The body must be
    valid JSON; the Authorization header (if present) is forwarded
    into ``params.token``.

    Returns the response payload directly — FastAPI serialises it
    to JSON. For a notification (no ``id``), we still return an
    empty object so HTTP-fetch clients don't choke on a 204.
    """
    try:
        body = await request.json()
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": f"parse error: {e}"},
        }

    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if isinstance(body, dict):
            if isinstance(body.get("params"), dict):
                body["params"].setdefault("token", token)
            else:
                body["params"] = {"token": token}

    resp = await handle_request(body if isinstance(body, dict) else {})
    return resp or {}
