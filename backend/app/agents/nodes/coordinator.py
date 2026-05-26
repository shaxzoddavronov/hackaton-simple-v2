from __future__ import annotations

from app.agents.llm import get_llm
from app.agents.state import GraphState
from app.schemas.llm_io import IntentDecision

_SYSTEM = (
    "You are the routing brain of QueryMind, an NL-to-SQL assistant. "
    "Classify each user message into EXACTLY ONE intent.\n\n"
    "Definitions:\n"
    "- chitchat: greetings, capability questions, off-topic small talk. "
    "Examples: 'hi', 'what can you do?'\n"
    "- metadata: asks ONLY about the database SHAPE — which tables/columns "
    "exist. The user is NOT asking for any actual row, count, ranking, or "
    "aggregate value. Examples: 'what tables do I have?', 'list the columns "
    "of orders', 'which schemas are in this DB?'\n"
    "- data_query: asks for actual ROWS, counts, aggregates, rankings, "
    "filtered values, time-series, top-N, or a single chart. The answer "
    "requires running SQL. Examples: 'how many users?', 'top 5 customers "
    "by revenue', 'who is the most active user', 'orders last week', "
    "'average order value by region'. If a question can be answered by "
    "running SELECT against tables, it is data_query — NOT metadata.\n"
    "- dashboard: asks for a multi-panel overview / KPIs side by side / "
    "'show me a dashboard for X'.\n"
    "- clarify: ambiguous about WHICH workspace OR what the user wants. "
    "Only use this when the question itself can't be acted on.\n\n"
    "Heuristics:\n"
    "  * Words like 'how many', 'count', 'top', 'most', 'least', "
    "'eng', 'qancha', 'nechta', 'kim', 'who is', 'which' followed by an "
    "entity → almost always data_query.\n"
    "  * Only classify as metadata when the question literally asks about "
    "schema structure, not values.\n\n"
    "Also extract `workspace_hint` if the message names a specific "
    "connected DB."
)


async def run(state: GraphState) -> GraphState:
    msg = state.get("user_message", "")
    if not msg:
        return {"intent": "clarify", "error_message": "empty user message"}

    llm = get_llm()
    decision = await llm.structured(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": msg},
        ],
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
