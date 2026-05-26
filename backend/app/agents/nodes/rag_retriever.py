"""Semantic retrieval node.

Sits between ``schema_loader`` and ``query_planner``. Takes the user's
question, embeds it via Triton, and pulls top-K relevant chunks from the
``rag_chunks`` table. Writes two slots to the graph state:

  - ``retrieved_chunks``    : the full :class:`RetrievedChunk` list (for the
                              planner prompt, debugging, telemetry).
  - ``pruned_table_qnames`` : qualified names of distinct tables surfaced by
                              the retrieval. The planner already consumes
                              this slot, so we keep the existing contract.

If Triton is unreachable or returns zero hits, we **don't crash** — we leave
``pruned_table_qnames`` untouched so the planner falls back to whatever the
BM25 pruner already populated in ``schema_loader``.

This node only runs for ``intent in {data_query, dashboard}``. For metadata
and chitchat the planner is skipped entirely.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.state import GraphState
from app.config import settings
from app.services.rag.retriever import RetrievedChunk, retrieve

log = logging.getLogger(__name__)


async def run(state: GraphState) -> GraphState:
    intent = state.get("intent")
    if intent not in {"data_query", "dashboard"}:
        return {}

    workspace_id = state.get("resolved_workspace_id")
    user_message = state.get("user_message", "")
    if not user_message.strip():
        return {}

    sa_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    try:
        async with Session() as session:
            try:
                chunks = await retrieve(
                    session,
                    query=user_message,
                    workspace_id=workspace_id,
                    top_k=settings.RAG_TOP_K,
                    include_global=True,
                )
            except Exception:
                # Retriever already swallows Triton failures; this catches
                # the unexpected — keep the agent functional.
                log.exception("rag_retriever: retrieval crashed; using BM25 fallback")
                return {}
    finally:
        await sa_engine.dispose()

    if not chunks:
        return {}

    # Derive the table qnames from schema_table chunks. Preserve order so
    # the planner sees the most-relevant tables first.
    pruned: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        if c.kind != "schema_table":
            continue
        qn = _qname_from_chunk(c)
        if qn and qn not in seen:
            seen.add(qn)
            pruned.append(qn)

    out: GraphState = {"retrieved_chunks": [_chunk_to_dict(c) for c in chunks]}
    # Only override if RAG actually found tables; otherwise leave the
    # BM25 result alone.
    if pruned:
        out["pruned_table_qnames"] = pruned
    return out


def _qname_from_chunk(c: RetrievedChunk) -> str | None:
    md = c.metadata or {}
    schema = md.get("schema")
    table = md.get("table")
    if schema and table:
        return f"{schema}.{table}"
    # Fall back to source_key, which is "schema.table" for schema_table chunks.
    return c.source_key or None


def _chunk_to_dict(c: RetrievedChunk) -> dict[str, Any]:
    return {
        "id": c.id,
        "kind": c.kind,
        "source_key": c.source_key,
        "text": c.text,
        "score": c.score,
        "metadata": c.metadata,
    }


__all__ = ["run"]
