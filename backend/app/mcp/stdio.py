"""MCP stdio transport — entrypoint for Claude Desktop integration.

Claude Desktop launches this as a subprocess; we read JSON-RPC
messages from stdin, dispatch via :mod:`app.mcp.server`, write
responses to stdout. One message per line (newline-delimited JSON).

Usage in ``claude_desktop_config.json``::

    {
      "mcpServers": {
        "querymind": {
          "command": "python",
          "args": ["-m", "app.mcp.stdio"],
          "env": {
            "QM_TOKEN": "<bearer JWT from /auth/login>",
            "DATABASE_URL": "postgresql+asyncpg://...",
            "REDIS_URL": "redis://..."
          },
          "cwd": "/path/to/querymind/backend"
        }
      }
    }

The ``QM_TOKEN`` env var is injected as ``params.token`` on every
RPC call so the server's auth path picks it up identically to the
HTTP-with-Authorization-header flow.

The stdio loop is intentionally minimal — no batching, no
keep-alives, no extras. Claude Desktop only sends one request at a
time and waits for the reply before sending the next.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from app.mcp.server import handle_request

# stdio uses stdout for the protocol; route logs to stderr so they
# don't corrupt the JSON stream.
logging.basicConfig(
    level=os.environ.get("QM_MCP_LOG", "INFO"),
    stream=sys.stderr,
    format="%(asctime)s [mcp-stdio] %(levelname)s %(message)s",
)
log = logging.getLogger("app.mcp.stdio")


async def _run() -> None:
    token = os.environ.get("QM_TOKEN", "")
    if not token:
        log.warning(
            "QM_TOKEN env var is empty — every tool call will "
            "respond with -32001 (unauthorized). Set it to the JWT "
            "from POST /auth/login."
        )

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    proto = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: proto, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            # EOF — Claude Desktop closed the pipe; clean exit.
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            err = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": f"parse error: {e}",
                },
            }
            _write(err)
            continue

        # Inject the env-var token into params.token so server.py's
        # auth path can pick it up uniformly. We don't override an
        # explicitly-passed token (lets a test client override).
        if isinstance(req.get("params"), dict):
            req["params"].setdefault("token", token)
        elif token:
            req["params"] = {"token": token}

        resp = await handle_request(req)
        if resp is not None:
            _write(resp)


def _write(payload: dict[str, Any]) -> None:
    """Write one JSON-RPC message to stdout as a single line, then
    flush — Claude Desktop reads the response synchronously and
    won't see it without a flush."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
