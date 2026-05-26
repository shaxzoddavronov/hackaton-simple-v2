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

    user_msg = Message(
        session_id=chat_session.id,
        role="user",
        content=payload.message,
    )
    session.add(user_msg)
    await session.commit()
    await session.refresh(chat_session)

    async def event_stream() -> AsyncIterator[bytes]:
        graph = get_graph()
        graph_input = {
            "user_id": current_user.id,
            "session_id": chat_session.id,
            "user_message": payload.message,
            "active_workspace_id": payload.active_workspace_id,
            "resolved_workspace_id": workspace_id,
        }

        yield _sse(
            "session",
            {
                "session_id": str(chat_session.id),
                "workspace_id": str(workspace_id) if workspace_id else None,
            },
        )

        final_state: dict[str, Any] = {}
        try:
            async for event in graph.astream(graph_input):
                # `event` is {node_name: state_delta}
                for node_name, delta in event.items():
                    yield _sse("node", {"node": node_name})
                    if isinstance(delta, dict):
                        for k, v in delta.items():
                            final_state[k] = v
        except Exception as exc:
            log.exception("graph invocation failed")
            yield _sse("error", {"message": str(exc)})
            return

        ui_spec = final_state.get("ui_spec")
        sql_executed = final_state.get("sql_executed")

        # Persist the assistant turn + audit row.
        assistant_msg = Message(
            session_id=chat_session.id,
            role="assistant",
            content=_extract_body(ui_spec),
            ui_spec=ui_spec.model_dump(mode="json") if ui_spec is not None else None,
        )
        session.add(assistant_msg)
        await session.flush()
        if sql_executed:
            rs = final_state.get("result")
            session.add(
                QueryHistory(
                    message_id=assistant_msg.id,
                    sql_text=sql_executed,
                    dialect=final_state.get("plan").dialect if final_state.get("plan") else "postgres",
                    took_ms=rs.took_ms if rs is not None else None,
                    row_count=rs.row_count if rs is not None else None,
                    status="ok" if rs is not None else "executor_error",
                )
            )
        await session.commit()

        yield _sse(
            "final",
            {
                "ui_spec": ui_spec.model_dump(mode="json") if ui_spec is not None else None,
                "sql": sql_executed,
                "assistant_message_id": str(assistant_msg.id),
            },
        )

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
