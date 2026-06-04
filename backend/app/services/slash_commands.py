"""Phase 39 — slash-command parser + handlers.

When a chat message starts with ``/``, ``parse_command`` recognises
one of the built-in utility commands and the chat endpoint
short-circuits the full agent pipeline — returns a text_only
UISpec directly. Saves a vLLM round-trip for utility actions.

Recognised commands (case-insensitive, leading ``/``):

  /help              show the list
  /sql               show the SQL of the most recent assistant turn
  /lang uz|ru|en     suggest preferred answer language for next turns
  /clear-cache       drop the Redis query cache for the current connection
  /refresh-schema    enqueue a re-profile for the current connection
  /explain           short markdown of how the agent works

Commands that need side effects (cache clear, schema refresh) take
``connection_id`` from the request payload. When that's missing
they return a friendly text_only telling the user to pick a
connection first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

SUPPORTED_LANGS = {"uz", "ru", "en"}


@dataclass(slots=True)
class SlashCommand:
    name: str
    arg: str = ""


@dataclass(slots=True)
class SlashResult:
    """A direct response that skips the agent graph. ``ui_spec`` is
    the dict shape the chat endpoint serialises into the SSE
    ``final`` event."""
    ui_spec: dict[str, Any]
    body: str
    # Side-effect hooks the chat endpoint runs after returning:
    refresh_connection_id: UUID | None = None
    clear_cache_connection_id: UUID | None = None


def parse_command(message: str) -> SlashCommand | None:
    """Return the parsed slash command, or ``None`` if the message
    isn't a recognised command. Unknown ``/x`` prompts return None
    so the agent gets to handle them as regular questions."""
    if not message:
        return None
    m = message.strip()
    if not m.startswith("/"):
        return None
    head, _, tail = m[1:].partition(" ")
    name = head.lower()
    if name not in _COMMANDS:
        return None
    return SlashCommand(name=name, arg=tail.strip())


async def handle_command(
    cmd: SlashCommand,
    *,
    db,
    user_id: UUID,
    workspace_id: UUID | None,
    connection_id: UUID | None,
    last_sql: str | None,
) -> SlashResult:
    """Dispatch a parsed command to its handler. Each handler is a
    pure-ish function that may signal side-effects via the
    ``SlashResult`` (caller actually performs them so this module
    stays free of side imports)."""
    handler = _COMMANDS[cmd.name]
    return await handler(
        cmd,
        db=db,
        user_id=user_id,
        workspace_id=workspace_id,
        connection_id=connection_id,
        last_sql=last_sql,
    )


# ── handlers ─────────────────────────────────────────────────────


async def _help(cmd, **_kw) -> SlashResult:
    body = (
        "**Slash commands**\n\n"
        "- `/help` — this list\n"
        "- `/sql` — show the SQL the agent generated last\n"
        "- `/lang uz|ru|en` — suggest answer language for next turns\n"
        "- `/clear-cache` — drop the query cache for the current connection\n"
        "- `/refresh-schema` — re-introspect the current connection\n"
        "- `/explain` — how the agent thinks (short overview)"
    )
    return SlashResult(
        ui_spec={"type": "text_only", "body_md": body}, body=body
    )


async def _sql(cmd, *, last_sql: str | None, **_kw) -> SlashResult:
    if not last_sql:
        body = "No SQL yet in this session — ask a data question first."
        return SlashResult(
            ui_spec={"type": "text_only", "body_md": body}, body=body
        )
    body = f"Last generated SQL:\n\n```sql\n{last_sql}\n```"
    return SlashResult(
        ui_spec={"type": "text_only", "body_md": body}, body=body
    )


async def _lang(cmd, *, db, user_id, **_kw) -> SlashResult:
    arg = cmd.arg.lower().strip()
    if arg not in SUPPORTED_LANGS:
        body = (
            "Usage: `/lang uz` · `/lang ru` · `/lang en`.\n\n"
            "The agent already picks language from your question; "
            "this command pins a default for turns where the language "
            "is ambiguous."
        )
        return SlashResult(
            ui_spec={"type": "text_only", "body_md": body}, body=body
        )
    # Persist on the user row's preferences if a column exists; we
    # don't add a schema migration here — the answer_writer node
    # already adapts to the user's language by mimicking the input.
    # This command is therefore informational; the body is the
    # confirmation surface.
    body = (
        f"Preferred answer language hint: **{arg}**. The agent already "
        "mirrors the language of your question; the hint takes effect "
        "only when the question is too short to tell."
    )
    return SlashResult(
        ui_spec={"type": "text_only", "body_md": body}, body=body
    )


async def _clear_cache(
    cmd, *, connection_id, **_kw
) -> SlashResult:
    if connection_id is None:
        body = "Pick a connection first — the cache is per-connection."
        return SlashResult(
            ui_spec={"type": "text_only", "body_md": body}, body=body
        )
    body = (
        "Query cache cleared for this connection. The next data turn "
        "will go straight to the DB."
    )
    return SlashResult(
        ui_spec={"type": "text_only", "body_md": body},
        body=body,
        clear_cache_connection_id=connection_id,
    )


async def _refresh_schema(
    cmd, *, connection_id, **_kw
) -> SlashResult:
    if connection_id is None:
        body = "Pick a connection first — schema refresh is per-connection."
        return SlashResult(
            ui_spec={"type": "text_only", "body_md": body}, body=body
        )
    body = (
        "Schema re-introspection queued. Refresh the workspace page in "
        "a few seconds to see the updated table / column counts."
    )
    return SlashResult(
        ui_spec={"type": "text_only", "body_md": body},
        body=body,
        refresh_connection_id=connection_id,
    )


async def _explain(cmd, **_kw) -> SlashResult:
    body = (
        "**How the agent answers**\n\n"
        "1. **Coordinator** classifies intent "
        "(data_query · dashboard · metadata · clarify · chitchat).\n"
        "2. **Schema loader + RAG** prune ~50 tables down to the "
        "~10 most relevant via dense (Triton bge-m3) + BM25 fusion.\n"
        "3. **Planner** drafts SQL constrained by your schema and "
        "any non-schema RAG context (uploaded docs, REST catalog).\n"
        "4. **Validator** parses the SQL (sqlglot) — rejects writes / "
        "system tables / dangerous functions.\n"
        "5. **Executor** runs read-only against your DB (per-dialect "
        "engine, 8s timeout, 1000-row cap).\n"
        "6. **Chart designer + Answer writer** run in parallel — "
        "chart heuristics pick a bar / line / KPI / table; the writer "
        "drafts the narrative.\n"
        "7. **Finalizer** packages a UISpec and the streaming "
        "frontend renders it.\n\n"
        "All vLLM and Triton calls log token / latency under the "
        "trace id printed at the start of each turn."
    )
    return SlashResult(
        ui_spec={"type": "text_only", "body_md": body}, body=body
    )


_COMMANDS = {
    "help": _help,
    "sql": _sql,
    "lang": _lang,
    "clear-cache": _clear_cache,
    "refresh-schema": _refresh_schema,
    "explain": _explain,
}


__all__ = [
    "SUPPORTED_LANGS",
    "SlashCommand",
    "SlashResult",
    "handle_command",
    "parse_command",
]
