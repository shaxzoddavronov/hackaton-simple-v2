from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import get_graph
from app.api.deps import get_current_user
from app.db.models import ChatSession, Message, QueryHistory, User, Workspace
from app.db.session import get_db
from app.services.workspace_resolver import (
    Ambiguous,
    Conflict,
    Missing,
    Resolved,
    resolve,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    session_id: UUID | None = None
    active_workspace_id: UUID | None = None
    # Picked from the per-workspace connection dropdown. Required for
    # data_query / dashboard turns; for chitchat / metadata the agent
    # can still respond without a specific DB.
    active_connection_id: UUID | None = None


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode("utf-8")


async def _resolve_or_workspace_id(
    session: AsyncSession, user: User, payload: ChatRequest
) -> UUID | None:
    """Returns a UUID or None. None means we need user clarification."""
    rows = await session.execute(
        select(Workspace).where(Workspace.owner_id == user.id)
    )
    ws_list = list(rows.scalars().all())
    if not ws_list:
        return None
    res = resolve(payload.message, payload.active_workspace_id, ws_list)
    if isinstance(res, Resolved):
        return res.workspace_id
    if isinstance(res, (Ambiguous, Conflict, Missing)):
        return payload.active_workspace_id  # fall back to dropdown if any
    return None


async def _ensure_session(
    session: AsyncSession, user: User, sid: UUID | None, workspace_id: UUID
) -> ChatSession:
    # ``workspace_id`` is required (NOT NULL in DB). Callers MUST resolve
    # it before invoking us — see post_chat for the clarify fallback.
    if sid is not None:
        cs = await session.get(ChatSession, sid)
        if cs is not None and cs.user_id == user.id:
            return cs
    cs = ChatSession(
        id=uuid4(),
        user_id=user.id,
        workspace_id=workspace_id,
        title=None,
    )
    session.add(cs)
    await session.flush()
    return cs


@router.post("")
async def post_chat(
    payload: ChatRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    workspace_id = await _resolve_or_workspace_id(session, current_user, payload)
    # ChatSession.workspace_id is NOT NULL — we must resolve one before
    # creating a session row. When the resolver can't decide (no workspaces
    # yet, ambiguous mention, dropdown not set), reply with a clarify
    # text_only UISpec over SSE instead of crashing on the FK constraint.
    if workspace_id is None:
        async def _clarify_stream() -> AsyncIterator[bytes]:
            yield _sse("session", {"session_id": None, "workspace_id": None})
            yield _sse(
                "final",
                {
                    "ui_spec": {
                        "type": "text_only",
                        "body_md": (
                            "Iltimos, qaysi **workspace** ustida ishlashni "
                            "tanlang — yo dropdown'dan, yoki xabar matnida "
                            "`@workspace-nomi` yozib yuboring."
                        ),
                    }
                },
            )

        return StreamingResponse(_clarify_stream(), media_type="text/event-stream")

    chat_session = await _ensure_session(
        session, current_user, payload.session_id, workspace_id
    )
    # Remember the connection picked for this turn so reopening the
    # session restores the right DB selector.
    if payload.active_connection_id is not None:
        chat_session.connection_id = payload.active_connection_id

    # Load recent conversation history BEFORE appending the new user
    # message so the agent sees what came before. Last 10 turns (5 user
    # + 5 assistant) is enough for follow-ups like "show as chart"
    # without bloating prompts. Stored oldest → newest.
    history_rows = await session.execute(
        select(Message)
        .where(Message.session_id == chat_session.id)
        .order_by(Message.created_at.desc())
        .limit(10)
    )
    history_msgs = list(history_rows.scalars().all())
    conversation_history = [
        {"role": m.role, "content": m.content}
        for m in reversed(history_msgs)
        if m.role in ("user", "assistant") and m.content
    ]

    user_msg = Message(
        session_id=chat_session.id,
        role="user",
        content=payload.message,
    )
    session.add(user_msg)
    await session.commit()
    await session.refresh(chat_session)

    async def event_stream() -> AsyncIterator[bytes]:
        # Per-stream trace id so every log line for this request is
        # filterable in the uvicorn console — when a chunk-encoding
        # crash happens, this is the first thing to look up.
        trace = uuid4().hex[:8]
        log.info("[%s] chat.stream START session=%s workspace=%s connection=%s",
                 trace, chat_session.id, workspace_id, payload.active_connection_id)

        graph = get_graph()
        graph_input = {
            "user_id": current_user.id,
            "session_id": chat_session.id,
            "user_message": payload.message,
            "active_workspace_id": payload.active_workspace_id,
            "active_connection_id": payload.active_connection_id,
            "resolved_workspace_id": workspace_id,
            "resolved_connection_id": payload.active_connection_id,
            "conversation_history": conversation_history,
        }

        yield _sse(
            "session",
            {
                "session_id": str(chat_session.id),
                "workspace_id": str(workspace_id) if workspace_id else None,
                "connection_id": (
                    str(payload.active_connection_id)
                    if payload.active_connection_id
                    else None
                ),
            },
        )

        final_state: dict[str, Any] = {}
        try:
            async for event in graph.astream(graph_input):
                # `event` is {node_name: state_delta}
                for node_name, delta in event.items():
                    log.info("[%s] node=%s", trace, node_name)
                    yield _sse("node", {"node": node_name})
                    if isinstance(delta, dict):
                        for k, v in delta.items():
                            final_state[k] = v
        except Exception as exc:
            log.exception("[%s] graph invocation failed", trace)
            yield _sse("error", {"message": str(exc)})
            return

        ui_spec = final_state.get("ui_spec")
        sql_executed = final_state.get("sql_executed")
        log.info(
            "[%s] graph done. ui_spec_type=%s sql_executed_len=%s has_plan=%s",
            trace,
            getattr(ui_spec, "type", None),
            len(sql_executed) if sql_executed else 0,
            final_state.get("plan") is not None,
        )

        # Persist the assistant turn + audit row.
        assistant_msg = Message(
            session_id=chat_session.id,
            role="assistant",
            content=_extract_body(ui_spec),
            ui_spec=ui_spec.model_dump(mode="json") if ui_spec is not None else None,
        )
        log.info("[%s] persist: session.add(assistant_msg)", trace)
        session.add(assistant_msg)
        log.info("[%s] persist: session.flush()", trace)
        await session.flush()
        if sql_executed:
            rs = final_state.get("result")
            plan = final_state.get("plan")
            audit_dialect = plan.dialect if plan is not None else "postgres"
            log.info(
                "[%s] persist: QueryHistory(dialect=%s, status=%s)",
                trace,
                audit_dialect,
                "ok" if rs is not None else "executor_error",
            )
            session.add(
                QueryHistory(
                    message_id=assistant_msg.id,
                    sql_text=sql_executed,
                    dialect=audit_dialect,
                    took_ms=rs.took_ms if rs is not None else None,
                    row_count=rs.row_count if rs is not None else None,
                    status="ok" if rs is not None else "executor_error",
                )
            )
        log.info("[%s] persist: session.commit()", trace)
        await session.commit()
        log.info("[%s] persist OK assistant_msg=%s", trace, assistant_msg.id)

        # Federation transparency: include the per-sub-query breakdown so
        # the UI can show "Queried: pg-quiz · 12 rows, es-search · 30 rows"
        # above the chart. Empty / missing on single-DB turns.
        sub_results = final_state.get("sub_results") or {}
        yield _sse(
            "final",
            {
                "ui_spec": ui_spec.model_dump(mode="json") if ui_spec is not None else None,
                "sql": sql_executed,
                "assistant_message_id": str(assistant_msg.id),
                "sub_results": sub_results,
            },
        )
        log.info("[%s] chat.stream END", trace)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _extract_body(ui_spec) -> str:
    if ui_spec is None:
        return ""
    if getattr(ui_spec, "type", None) == "text_only":
        return getattr(ui_spec, "body_md", "")
    if getattr(ui_spec, "type", None) == "dashboard":
        for ch in getattr(ui_spec, "children", []):
            if getattr(ch.spec, "type", None) == "text_only":
                return getattr(ch.spec, "body_md", "")
    return getattr(ui_spec, "title", "") or ""


@router.get("/sessions")
async def list_sessions(
    workspace_id: UUID | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List the calling user's past chat sessions.

    Filterable by ``workspace_id`` so the chat sidebar can show only the
    history for the workspace currently selected. Title comes from the
    first user message (truncated to 60 chars) — we don't store an
    explicit title yet.
    """
    stmt = select(ChatSession).where(ChatSession.user_id == current_user.id)
    if workspace_id is not None:
        stmt = stmt.where(ChatSession.workspace_id == workspace_id)
    stmt = stmt.order_by(ChatSession.created_at.desc()).limit(min(limit, 200))
    rows = await session.execute(stmt)
    sessions = list(rows.scalars().all())

    # Fetch the first user message per session in one query so we can
    # synthesize a preview/title without N+1 round-trips.
    out: list[dict[str, Any]] = []
    for cs in sessions:
        first = await session.execute(
            select(Message)
            .where(Message.session_id == cs.id, Message.role == "user")
            .order_by(Message.created_at)
            .limit(1)
        )
        first_msg = first.scalar_one_or_none()
        last = await session.execute(
            select(Message)
            .where(Message.session_id == cs.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        last_msg = last.scalar_one_or_none()
        title = cs.title or (
            (first_msg.content[:60] + ("…" if len(first_msg.content) > 60 else ""))
            if first_msg
            else "(empty)"
        )
        out.append(
            {
                "id": str(cs.id),
                "workspace_id": str(cs.workspace_id),
                "title": title,
                "created_at": cs.created_at.isoformat(),
                "last_message_at": (
                    last_msg.created_at.isoformat() if last_msg else cs.created_at.isoformat()
                ),
            }
        )
    return out


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cs = await session.get(ChatSession, session_id)
    if cs is None or cs.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    rows = await session.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
    )
    msgs = rows.scalars().all()
    return {
        "session_id": str(cs.id),
        "workspace_id": str(cs.workspace_id) if cs.workspace_id else None,
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "ui_spec": m.ui_spec,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    cs = await session.get(ChatSession, session_id)
    if cs is None or cs.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    await session.delete(cs)
    await session.commit()
    # messages + query_history cascade via FK ON DELETE CASCADE.
