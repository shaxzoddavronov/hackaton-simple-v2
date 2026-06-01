"""Model Context Protocol (MCP) server for QueryMind.

Phase 31 — exposes QueryMind's workspace + query surface as
MCP tools so external LLM clients (Claude Desktop, ChatGPT
desktop, IDE assistants supporting MCP) can call it directly.

Three transports we care about:

  * **stdio** — the canonical Claude Desktop integration. The
    user adds a snippet to ``claude_desktop_config.json`` pointing
    at ``python -m app.mcp.stdio`` and gets the QueryMind tools in
    their chat sidebar.

  * **HTTP / SSE** — hosted MCP for remote desktops + cloud
    assistants. Mounted at ``/mcp`` by the FastAPI app when
    ``QM_MCP_ENABLED=true``.

  * **WebSocket** — out of scope for v1; HTTP / SSE covers
    everything we need.

The exposed tools mirror the existing REST API but in
MCP-tool-shape (one input schema, one output payload):

  * ``querymind.list_workspaces`` — enumerate the caller's
    workspaces.
  * ``querymind.workspace_schema`` — return a workspace's
    SchemaBundle (one per connection).
  * ``querymind.ask`` — natural-language question → final
    UISpec + sql + citations. Runs the same agent graph the
    chat endpoint uses.

Auth: an MCP call carries a QueryMind JWT in the standard
``Authorization`` header for HTTP transport, or in a
``QM_TOKEN`` env var for stdio. Without a valid token the
server returns the MCP error code ``-32001`` (unauthorized).
"""
from __future__ import annotations
