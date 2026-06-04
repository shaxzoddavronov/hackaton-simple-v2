from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import get_graph
from app.api.deps import get_current_user
from app.db.models import ChatSession, Message, QueryHistory, User, Workspace
from app.db.session import get_db
from app.limiter import limiter
from app.metrics import (
    chat_duration_seconds,
    chat_turns_total,
    query_history_total,
)
from app.config import settings
from app.services.result_export import captured_rows_for_export
from app.services.workspace_resolver import (
    Ambiguous,
    Conflict,
    Missing,
    Resolved,
    resolve,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


ChatScope = Literal[
    "table",            # narrowest — one table inside one connection
    "database",         # one connection (the canonical default)
    "all_databases",    # every connection in the workspace
    "cluster",          # every connection in one ConnectionCluster
    "all_clusters",     # every connection that belongs to ANY cluster
    "all_connections",  # synonym for all_databases (every conn period)
]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    session_id: UUID | None = None
    active_workspace_id: UUID | None = None
    # Picked from the per-workspace connection dropdown. Required for
    # data_query / dashboard turns; for chitchat / metadata the agent
    # can still respond without a specific DB.
    active_connection_id: UUID | None = None
    # Phase 42 — scope of the question. ``database`` is the legacy
    # default (one connection). Wider scopes trigger the federation
    # path with the expanded connection set.
    scope: ChatScope = "database"
    # When scope == "table" the agent restricts schema_loader to this
    # single qualified name (e.g. "public.orders"). Ignored for other
    # scopes.
    scope_table: str | None = Field(default=None, max_length=256)
    # When scope == "cluster" this is the target cluster id.
    scope_cluster_id: UUID | None = None


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode("utf-8")


def _sanitize_error_for_client(exc: Exception, trace: str) -> str:
    """Strip internal details out of an SSE error payload.

    Raw exceptions leak SQL, connection strings, stack-trace cues, and
    sometimes credentials (an asyncpg auth error includes the user
    name). The server still logs the full traceback under the same
    trace id, so support can correlate.

    Heuristic by exception type:
      * ``ValueError`` raised by our own engine guard (message starts
        with "Refusing to execute") is already user-friendly — pass
        through as-is.
      * Async-DB / driver errors → generic "data source error".
      * Anything else → generic "internal error".
    """
    msg = str(exc) or exc.__class__.__name__
    if isinstance(exc, ValueError) and msg.startswith("Refusing to execute"):
        return msg
    cls = type(exc).__name__
    if cls in {"ProgrammingError", "OperationalError", "IntegrityError",
               "InterfaceError", "DatabaseError", "InvalidTextRepresentation"}:
        return f"Data source error (request_id={trace})"
    if cls in {"APIError", "APIConnectionError", "APIStatusError",
               "AuthenticationError", "RateLimitError", "Timeout"}:
        return f"AI service error (request_id={trace})"
    if cls == "ValidationError":  # pydantic
        return f"AI returned malformed output (request_id={trace})"
    return f"Internal error (request_id={trace})"


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
@limiter.limit("10/minute")
async def post_chat(
    request: Request,  # slowapi requires Request as the first positional arg
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
    # message so the agent sees what came before. Phase 36: if the
    # session has accumulated > SUMMARY_THRESHOLD messages, fold the
    # OLDEST half into a persistent summary and prepend it to the
    # agent's history instead of feeding raw old turns. Keeps prompt
    # budget bounded across long sessions.
    from app.services.conversation_summary import (
        KEEP_RECENT,
        ensure_summary,
    )

    summary_text = await ensure_summary(session, chat_session)
    history_rows = await session.execute(
        select(Message)
        .where(Message.session_id == chat_session.id)
        .order_by(Message.created_at.desc())
        .limit(KEEP_RECENT)
    )
    history_msgs = list(history_rows.scalars().all())
    conversation_history: list[dict[str, str]] = []
    if summary_text:
        conversation_history.append(
            {
                "role": "system",
                "content": (
                    "Summary of earlier conversation in this session:\n"
                    + summary_text
                ),
            }
        )
    conversation_history.extend(
        {"role": m.role, "content": m.content}
        for m in reversed(history_msgs)
        if m.role in ("user", "assistant") and m.content
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
        # Per-stream trace id so every log line for this request is
        # filterable in the uvicorn console — when a chunk-encoding
        # crash happens, this is the first thing to look up.
        trace = uuid4().hex[:8]
        log.info("[%s] chat.stream START session=%s workspace=%s connection=%s",
                 trace, chat_session.id, workspace_id, payload.active_connection_id)

        # Metric bookkeeping: wall-clock start + a label we flip in the
        # except block. ``final_state`` is defined here so the ``finally``
        # below can read the resolved intent regardless of which branch
        # ran.
        _t0 = time.perf_counter()
        status_label = "ok"
        final_state: dict[str, Any] = {}

        # Phase 37 — open a usage bucket bound to this request's
        # async context. LLM calls, query_executor, rag_retriever
        # and the query cache all increment counters on it; the
        # ``finally`` block UPSERTs into ``usage_daily``.
        from app.services.usage import (
            clear_bucket,
            flush_bucket,
            start_bucket,
        )

        usage_bucket = start_bucket(str(workspace_id))

        # Phase 39 — slash-command short-circuit. If the user typed
        # /sql, /help, /clear-cache, ... we handle it here without
        # spinning up the agent graph or paying a vLLM round-trip.
        from app.services.slash_commands import (
            handle_command,
            parse_command,
        )

        slash = parse_command(payload.message)
        if slash is not None:
            # Pull the most recent SQL the agent produced in this
            # session so /sql can echo it without re-running.
            last_sql_row = (
                await session.execute(
                    select(QueryHistory)
                    .join(Message, Message.id == QueryHistory.message_id)
                    .where(Message.session_id == chat_session.id)
                    .order_by(QueryHistory.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            last_sql = last_sql_row.sql_text if last_sql_row else None

            result = await handle_command(
                slash,
                db=session,
                user_id=current_user.id,
                workspace_id=workspace_id,
                connection_id=payload.active_connection_id,
                last_sql=last_sql,
            )

            # Run any side-effect the handler requested. Each runs
            # behind a defensive try/except so a Redis hiccup or a
            # missing module never breaks the chat path.
            if result.clear_cache_connection_id:
                try:
                    from app.services.query_cache import (
                        invalidate_connection,
                    )

                    await invalidate_connection(
                        str(result.clear_cache_connection_id)
                    )
                except Exception:  # noqa: BLE001
                    log.exception("[%s] /clear-cache failed", trace)
            if result.refresh_connection_id:
                try:
                    from app.db.models import ProfileJob

                    job = ProfileJob(
                        connection_id=result.refresh_connection_id,
                        state="queued",
                    )
                    session.add(job)
                    await session.commit()
                except Exception:  # noqa: BLE001
                    log.exception("[%s] /refresh-schema enqueue failed", trace)

            # Persist the assistant turn so /sql, /lang etc. show up
            # in the chat history just like a normal answer.
            assistant_msg = Message(
                session_id=chat_session.id,
                role="assistant",
                content=result.body,
                ui_spec=result.ui_spec,
            )
            session.add(assistant_msg)
            await session.commit()
            await session.refresh(assistant_msg)

            yield _sse(
                "session",
                {
                    "session_id": str(chat_session.id),
                    "workspace_id": (
                        str(workspace_id) if workspace_id else None
                    ),
                    "connection_id": (
                        str(payload.active_connection_id)
                        if payload.active_connection_id
                        else None
                    ),
                },
            )
            yield _sse(
                "final",
                {
                    "ui_spec": result.ui_spec,
                    "sql": None,
                    "assistant_message_id": str(assistant_msg.id),
                    "sub_results": {},
                    "citations": [],
                },
            )
            # Skip the rest of the graph path — slash commands are
            # their own terminal flow.
            final_state["intent"] = "slash_command"
            return

        try:
            graph = get_graph()
            # Phase 42 — resolve the requested scope into a concrete
            # set of connection ids. For `database` (the default) we
            # don't actually need to hit the DB; the active conn id
            # is the answer. For wider scopes we look up cluster
            # members or every workspace connection.
            from app.services.scope_resolver import resolve_scope

            scope_resolution = None
            if workspace_id is not None:
                scope_resolution = await resolve_scope(
                    session,
                    workspace_id=workspace_id,
                    scope=payload.scope,
                    active_connection_id=payload.active_connection_id,
                    scope_cluster_id=payload.scope_cluster_id,
                )
                if scope_resolution.error and payload.scope != "database":
                    # Wider scope can't be resolved → tell the user
                    # plainly and bail. `database` falls through
                    # because the legacy behaviour also tolerates a
                    # missing active connection on chitchat turns.
                    yield _sse(
                        "session",
                        {
                            "session_id": str(chat_session.id),
                            "workspace_id": (
                                str(workspace_id)
                                if workspace_id
                                else None
                            ),
                            "connection_id": None,
                        },
                    )
                    yield _sse(
                        "final",
                        {
                            "ui_spec": {
                                "type": "text_only",
                                "body_md": (
                                    f"Couldn't resolve scope "
                                    f"`{payload.scope}`: "
                                    + scope_resolution.error
                                ),
                            },
                            "sql": None,
                            "assistant_message_id": "",
                            "sub_results": {},
                            "citations": [],
                        },
                    )
                    final_state["intent"] = "clarify"
                    return

            scope_ids = (
                scope_resolution.connection_ids
                if scope_resolution
                else []
            )
            federate = bool(
                scope_resolution and scope_resolution.federation
            )

            graph_input = {
                "user_id": current_user.id,
                "session_id": chat_session.id,
                "user_message": payload.message,
                "active_workspace_id": payload.active_workspace_id,
                "active_connection_id": payload.active_connection_id,
                "resolved_workspace_id": workspace_id,
                "resolved_connection_id": payload.active_connection_id,
                "conversation_history": conversation_history,
                # Phase 42 — federation routing + ids the multi-
                # schema loader uses to bound its scan.
                "scope": payload.scope,
                "scope_table": payload.scope_table,
                "scope_connection_ids": scope_ids,
            }
            # When the scope is wider than `database`, override the
            # coordinator-decided intent so the graph routes through
            # multi_schema_loader → federated_planner → federated_executor.
            if federate:
                graph_input["intent"] = "federated_query"

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

            # Phase 38 — emit "you asked this before" hits BEFORE the
            # agent runs so the chip lights up immediately. Failures
            # (Triton down, no prior rows) just yield an empty list.
            if workspace_id is not None:
                from app.services.qa_history import find_similar

                try:
                    hits = await find_similar(
                        session,
                        workspace_id=workspace_id,
                        question=payload.message,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("[%s] qa_history.find_similar failed", trace)
                    hits = []
                if hits:
                    yield _sse(
                        "similar",
                        {
                            "hits": [
                                {
                                    "message_id": h.message_id,
                                    "session_id": h.session_id,
                                    "question": h.question,
                                    "headline": h.headline,
                                    "similarity": h.similarity,
                                }
                                for h in hits
                            ]
                        },
                    )

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
                status_label = "error"
                yield _sse(
                    "error",
                    {"message": _sanitize_error_for_client(exc, trace)},
                )
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
                audit_status = "ok" if rs is not None else "executor_error"
                log.info(
                    "[%s] persist: QueryHistory(dialect=%s, status=%s)",
                    trace,
                    audit_dialect,
                    audit_status,
                )
                # Phase 34 — cache rows for export-as-CSV/XLSX/JSON.
                # Drop the cache silently when over budget so a single
                # huge query doesn't bloat the metadata DB.
                exp_cols, exp_rows = captured_rows_for_export(
                    rs,
                    max_rows=settings.RESULT_EXPORT_MAX_ROWS,
                    max_bytes=settings.RESULT_EXPORT_MAX_BYTES,
                )
                session.add(
                    QueryHistory(
                        message_id=assistant_msg.id,
                        sql_text=sql_executed,
                        dialect=audit_dialect,
                        took_ms=rs.took_ms if rs is not None else None,
                        row_count=rs.row_count if rs is not None else None,
                        status=audit_status,
                        result_columns=exp_cols,
                        result_rows=exp_rows,
                    )
                )
                # Cardinality note: ``dialect`` is the small enum from the
                # engine registry (postgres/sqlite/mysql/clickhouse/...);
                # ``status`` is the same finite set we persist. Safe.
                query_history_total.labels(
                    dialect=audit_dialect,
                    status=audit_status,
                ).inc()
            log.info("[%s] persist: session.commit()", trace)
            await session.commit()
            log.info("[%s] persist OK assistant_msg=%s", trace, assistant_msg.id)

            # Phase 38 — index this Q-A pair for the next turn's
            # "you asked this before" search. Only when we actually
            # answered something useful (ui_spec present + headline
            # extractable) and we're not on a chitchat / clarify
            # turn. Triton failures are silent — recall is a
            # progressive enhancement.
            if (
                ui_spec is not None
                and workspace_id is not None
                and str(final_state.get("intent") or "")
                in ("data_query", "dashboard", "metadata", "federated_query")
            ):
                try:
                    from app.services.qa_history import index_qa_pair

                    headline = _extract_headline(ui_spec)
                    await index_qa_pair(
                        session,
                        workspace_id=workspace_id,
                        message_id=assistant_msg.id,
                        session_id=chat_session.id,
                        question=payload.message,
                        headline=headline,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("[%s] qa_history.index_qa_pair failed", trace)

            # Federation transparency: include the per-sub-query breakdown so
            # the UI can show "Queried: pg-quiz · 12 rows, es-search · 30 rows"
            # above the chart. Empty / missing on single-DB turns.
            sub_results = final_state.get("sub_results") or {}
            citations = final_state.get("citations") or []
            yield _sse(
                "final",
                {
                    "ui_spec": ui_spec.model_dump(mode="json") if ui_spec is not None else None,
                    "sql": sql_executed,
                    "assistant_message_id": str(assistant_msg.id),
                    "sub_results": sub_results,
                    "citations": citations,
                },
            )
            log.info("[%s] chat.stream END", trace)
        finally:
            # Always emit chat-turn metrics — happy path, agent-error path,
            # AND client-disconnect path (StreamingResponse closes the
            # generator). ``intent`` may be missing if we crashed before
            # the coordinator ran; bucket as "unknown" so the label is
            # bounded.
            chat_turns_total.labels(
                intent=str(final_state.get("intent") or "unknown"),
                status=status_label,
            ).inc()
            chat_duration_seconds.observe(time.perf_counter() - _t0)
            # Phase 37 — UPSERT the per-day counters and unbind the
            # ContextVar so a follow-up request in the same async
            # worker doesn't inherit stale state. Both the flush
            # and the clear are wrapped — usage tracking must never
            # crash the chat path even on DB failure.
            try:
                await flush_bucket(session, usage_bucket)
            except Exception:  # pragma: no cover
                log.exception("[%s] usage.flush_bucket failed", trace)
            finally:
                clear_bucket()

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


def _extract_headline(ui_spec) -> str:
    """Pull a short headline from a UISpec for the qa_history embed.

    The chart types carry a ``title`` attribute; KPI carries a
    ``label`` + ``value`` we paste together; text_only's body_md
    becomes the headline truncated to the first line. Dashboards
    recurse into the first child whose spec yields a non-empty
    headline.
    """
    if ui_spec is None:
        return ""
    t = getattr(ui_spec, "type", None)
    if t == "kpi":
        label = getattr(ui_spec, "label", "")
        value = getattr(ui_spec, "value", "")
        if label and value not in ("", None):
            return f"{label}: {value}"
        return str(label or value or "")
    if t == "text_only":
        body = (getattr(ui_spec, "body_md", "") or "").strip()
        # First non-empty line, capped.
        for line in body.splitlines():
            line = line.strip()
            if line:
                return line[:200]
        return ""
    if t == "dashboard":
        for ch in getattr(ui_spec, "children", []):
            inner = _extract_headline(ch.spec)
            if inner:
                return inner
        return getattr(ui_spec, "title", "") or ""
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


# ── Phase 34 — query result export ────────────────────────────────────


_EXPORT_FORMATS = {
    "csv": ("text/csv; charset=utf-8", "csv"),
    "json": ("application/json", "json"),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet",
        "xlsx",
    ),
}


@router.get("/messages/{message_id}/export")
async def export_message_result(
    message_id: UUID,
    format: str = "csv",
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Download the cached result rows of one assistant message.

    The result is the exact rows the agent produced when the message
    was first generated — we don't re-run the query, both for cost
    reasons and because federated turns store a multi-section SQL
    summary that isn't directly re-runnable.

    Authorization: only the user who owns the parent ChatSession can
    download. 404 (not 403) on miss to avoid leaking message ids
    across users.

    Status codes:
      * 200  — payload follows.
      * 404  — message doesn't exist, doesn't belong to the user, or
               has no QueryHistory row.
      * 410  — result_rows were dropped (oversize / older than the
               column). Re-run the question to refresh the cache.
      * 422  — unsupported ``format``.
    """
    fmt = (format or "csv").lower()
    if fmt not in _EXPORT_FORMATS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"format must be one of {sorted(_EXPORT_FORMATS)}",
        )

    msg = await session.get(Message, message_id)
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    cs = await session.get(ChatSession, msg.session_id)
    if cs is None or cs.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")

    qh = (
        await session.execute(
            select(QueryHistory).where(QueryHistory.message_id == message_id)
        )
    ).scalar_one_or_none()
    if qh is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No query result is associated with this message",
        )
    if not qh.result_columns or qh.result_rows is None:
        raise HTTPException(
            status.HTTP_410_GONE,
            "Result rows were not cached "
            "(too large or older than the export feature). "
            "Re-run the question to enable export.",
        )

    # Local imports to keep the heavy XLSX path out of module load.
    from app.services.result_export import to_csv, to_json, to_xlsx

    columns = list(qh.result_columns)
    rows = list(qh.result_rows)
    if fmt == "csv":
        body = to_csv(columns, rows)
    elif fmt == "json":
        body = to_json(columns, rows)
    else:
        body = to_xlsx(columns, rows)

    mime, ext = _EXPORT_FORMATS[fmt]
    filename = f"querymind-{str(message_id)[:8]}.{ext}"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(body)),
    }
    return StreamingResponse(
        iter([body]),
        media_type=mime,
        headers=headers,
    )
