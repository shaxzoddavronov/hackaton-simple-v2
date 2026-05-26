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
    active_workspace_id: UUID | None  # dropdown selection

    # Coordinator outputs
    resolved_workspace_id: UUID | None
    intent: str  # chitchat | metadata | data_query | dashboard | clarify
    workspace_hint: str | None

    # Schema loader / pruner
    schema_bundle: SchemaBundle | None
    pruned_table_qnames: list[str]

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
