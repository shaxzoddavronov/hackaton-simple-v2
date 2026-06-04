from __future__ import annotations

from typing import Annotated, Any, TypedDict
from uuid import UUID

from app.engines.base import ResultSet, SchemaBundle, ValidationResult
from app.schemas.llm_io import AnswerDraft, SqlPlan
from app.schemas.ui_spec import UISpec


def _take_last(_old: Any, new: Any) -> Any:
    """LangGraph reducer that prefers the latest non-None value."""
    return new if new is not None else _old


class GraphState(TypedDict, total=False):
    # Inputs
    user_id: UUID
    session_id: UUID
    user_message: str
    active_workspace_id: UUID | None  # workspace dropdown selection
    active_connection_id: UUID | None  # connection dropdown selection

    # Most recent N turns of this chat session, oldest first. Each item
    # is ``{"role": "user"|"assistant", "content": str}``. Coordinator
    # uses it to resolve follow-up references ("show as chart" → re-
    # visualize the previous turn). The planner consumes it for
    # multi-turn refinements. The answer writer uses it to match the
    # user's language.
    conversation_history: list[dict[str, str]]

    # Coordinator outputs
    resolved_workspace_id: UUID | None
    resolved_connection_id: UUID | None  # which DB to actually query
    intent: str  # chitchat | metadata | data_query | dashboard | clarify
    workspace_hint: str | None

    # Phase 42 — scope picker. Populated by the chat API from the
    # user's `scope` choice. When set, the federation path filters
    # its connection scan to only these ids (instead of "every ready
    # connection in the workspace"). For single-connection scopes
    # (`table`, `database`) this is `[active_connection_id]`; for
    # cluster / all_clusters / all_connections it expands.
    scope: str | None  # "table" | "database" | "cluster" | ...
    scope_connection_ids: list[UUID]
    scope_table: str | None  # narrowing for scope="table"

    # Schema loader / pruner
    schema_bundle: SchemaBundle | None
    pruned_table_qnames: list[str]

    # Federated path: multi-schema loader fills this dict on
    # ``intent == "federated_query"``. Keys are connection UUID strings;
    # values are the per-connection :class:`SchemaBundle`. The
    # federated_planner reads it; the single-connection paths ignore it.
    connection_bundles: dict[str, SchemaBundle]

    # Federated planner output (subset of FederatedPlan as dicts so
    # LangGraph's TypedDict semantics stay simple).
    federated_plan: dict[str, Any] | None

    # Per-sub-query results keyed by alias. The federated_executor
    # populates this and then runs the merge pipeline.
    sub_results: dict[str, Any]

    # RAG retrieval (semantic top-K via Triton + pgvector). When empty,
    # the planner falls back to the BM25 pruned list above. Each item is
    # the dict form of a `RetrievedChunk` (see services/rag/retriever.py).
    retrieved_chunks: list[dict[str, Any]]

    # Planner / validator / executor
    plan: SqlPlan | None
    validation: ValidationResult | None
    result: ResultSet | None
    sql_executed: str | None

    # Parallel fan-out outputs — LangGraph merges by replacing
    chart: Annotated[UISpec | None, _take_last]
    answer: Annotated[AnswerDraft | None, _take_last]

    # Citations attached by ``answer_writer`` from retrieved RAG chunks
    # (kinds: user_doc, harvested_doc). The chat SSE final event echoes
    # this list so the UI can render "Sources" under the answer.
    citations: Annotated[list[dict[str, Any]] | None, _take_last]

    # Finalizer output
    ui_spec: UISpec | None

    # Retry counters
    planner_attempts: int
    executor_attempts: int

    # Error reporting
    error_message: str | None
    last_validation_error: str | None
    last_executor_error: str | None

    # Telemetry
    latency_ms: dict[str, int]


__all__ = ["GraphState"]
