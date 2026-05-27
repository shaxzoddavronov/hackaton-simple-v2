from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.llm import get_llm
from app.agents.state import GraphState
from app.config import settings
from app.db.models import WorkspaceConnection
from app.schemas.llm_io import IntentDecision

log = logging.getLogger(__name__)

_SYSTEM_BASE = (
    "You are the routing brain of QueryMind, an NL-to-SQL assistant. "
    "Classify each user message into EXACTLY ONE intent.\n\n"
    "IMPORTANT — follow-up handling:\n"
    "If conversation_history is supplied, treat the new message as a "
    "POSSIBLE follow-up. Short phrases like 'show as chart', "
    "'grafik korinishda korsat', 'more detail', 'aniq qilib ber', "
    "'and broken down by X' are NOT new questions — they refer back to "
    "the immediately previous user/assistant pair. In that case classify "
    "based on the previous user's INTENT (typically data_query / "
    "dashboard), not the new short phrase. The planner will receive the "
    "same history and merge the request into one SQL.\n\n"
    "Definitions:\n"
    "- chitchat: greetings, capability questions, off-topic small talk. "
    "Examples: 'hi', 'what can you do?'\n"
    "- metadata: asks ONLY about the database SHAPE — which tables/columns "
    "exist. The user is NOT asking for any actual row, count, ranking, or "
    "aggregate value. Examples: 'what tables do I have?', 'list the columns "
    "of orders', 'which schemas are in this DB?'\n"
    "- data_query: asks for actual ROWS, counts, aggregates, rankings, "
    "filtered values, time-series, top-N, or a single chart against ONE "
    "database. Examples: 'how many users?', 'top 5 customers by revenue', "
    "'who is the most active user', 'orders last week'.\n"
    "- dashboard: asks for a multi-panel overview / KPIs side by side / "
    "'show me a dashboard for X' (still single DB).\n"
    "- federated_query: answer needs DATA FROM TWO OR MORE different "
    "connections in this workspace, joined or merged together. Triggers:\n"
    "    * the message names two or more connection names from the list "
    "below (e.g., 'compare orders in <conn-a> with events in <conn-b>'),\n"
    "    * cross-database language: 'join … with …', 'compare X across', "
    "'both databases', 'combine X and Y', 'side by side',\n"
    "    * different data clearly lives in different stores (postgres "
    "orders + ES logs, mongo events + sql users, etc.).\n"
    "  If the question can be answered by ONE connection alone, prefer "
    "data_query over federated_query.\n"
    "- clarify: ambiguous about WHICH workspace OR what the user wants. "
    "Only use this when the question itself can't be acted on.\n\n"
    "Heuristics:\n"
    "  * Words like 'how many', 'count', 'top', 'most', 'least', "
    "'eng', 'qancha', 'nechta', 'kim', 'who is', 'which' followed by an "
    "entity → almost always data_query (or federated_query if multiple "
    "DBs are referenced).\n"
    "  * Only classify as metadata when the question literally asks about "
    "schema structure, not values.\n\n"
    "Also extract `workspace_hint` if the message names a specific "
    "connected DB."
)


async def _connection_listing(workspace_id: UUID | None) -> str:
    """Render the user's workspace connections as a hint for the
    classifier. Without this the LLM can't reliably detect when a
    question names two specific connections (and thus warrants
    federated_query)."""
    if workspace_id is None:
        return ""
    sa_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    try:
        async with Session() as session:
            rows = await session.execute(
                select(WorkspaceConnection).where(
                    WorkspaceConnection.workspace_id == workspace_id,
                    WorkspaceConnection.status == "ready",
                )
            )
            conns = list(rows.scalars().all())
    finally:
        await sa_engine.dispose()
    if not conns:
        return ""
    lines = [f"  - {c.name} ({c.dialect})" for c in conns]
    return "\nConnections available in this workspace:\n" + "\n".join(lines)


async def run(state: GraphState) -> GraphState:
    msg = state.get("user_message", "")
    if not msg:
        return {"intent": "clarify", "error_message": "empty user message"}

    listing = await _connection_listing(state.get("resolved_workspace_id"))
    system_prompt = _SYSTEM_BASE + listing

    # Carry conversation history so the classifier can resolve
    # follow-ups against the previous turn.
    history = state.get("conversation_history") or []
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]
    # Send only the last 6 turns to keep the prompt tight — the
    # IntentDecision schema is small, so more history rarely helps.
    for h in history[-6:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if role not in ("user", "assistant") or not content:
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": msg})

    llm = get_llm()
    decision = await llm.structured(
        messages,
        IntentDecision,
    )
    out: GraphState = {
        "intent": decision.intent,
        "workspace_hint": decision.workspace_hint,
    }
    # Default resolved_workspace_id to whatever the dropdown picked. The
    # workspace_resolver is wired in by the API layer before invoking the
    # graph; here we just carry whatever's already in state.
    if "resolved_workspace_id" not in state:
        out["resolved_workspace_id"] = state.get("active_workspace_id")
    return out
