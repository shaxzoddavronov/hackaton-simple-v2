"""Top-K semantic retrieval over ``rag_chunks``.

The agent's ``rag_retriever`` node embeds the user message once, then this
module turns that vector into a list of :class:`RetrievedChunk`.

Postgres path: a single SQL query using pgvector's ``<=>`` cosine-distance
operator. Workspace-scoped filtering happens in SQL so the HNSW index does
the heavy lifting.

SQLite path: pull every chunk for the workspace, compute cosine in Python.
This is only used by unit tests and tiny demo databases, so the O(N) cost
is fine.

If Triton is unreachable we **don't crash** — we return an empty result and
let the caller fall back to BM25. That's what keeps the agent functional
during Triton restarts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RagChunk
from app.services.rag.triton_client import (
    TritonError,
    TritonUnavailable,
    get_client,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RetrievedChunk:
    id: str
    kind: str
    source_key: str
    text: str
    score: float
    metadata: dict[str, Any]


async def retrieve(
    session: AsyncSession,
    *,
    query: str,
    workspace_id: UUID | None,
    top_k: int = 12,
    include_global: bool = True,
) -> list[RetrievedChunk]:
    """Semantic top-K. Empty list on Triton failure (caller falls back).

    ``include_global`` includes ``workspace_id IS NULL`` chunks (our REST
    API catalog) alongside the workspace's own schema chunks.
    """
    if not query.strip():
        return []
    client = get_client()
    if not client.enabled:
        return []

    try:
        resp = await client.embed([query])
    except (TritonUnavailable, TritonError) as e:
        log.warning("RAG embedding failed (%s); skipping semantic retrieval", e)
        return []
    if not resp.vectors:
        return []
    qvec = resp.vectors[0]

    if _is_postgres(session):
        return await _retrieve_pgvector(
            session, qvec, workspace_id, top_k, include_global
        )
    return await _retrieve_sqlite(
        session, qvec, workspace_id, top_k, include_global
    )


async def _retrieve_pgvector(
    session: AsyncSession,
    qvec: list[float],
    workspace_id: UUID | None,
    top_k: int,
    include_global: bool,
) -> list[RetrievedChunk]:
    qvec_str = "[" + ",".join(f"{v:.7f}" for v in qvec) + "]"
    where_parts: list[str] = []
    params: dict[str, Any] = {"qvec": qvec_str, "k": top_k}
    if workspace_id is None:
        where_parts.append("workspace_id IS NULL")
    elif include_global:
        where_parts.append("(workspace_id = :wid OR workspace_id IS NULL)")
        params["wid"] = workspace_id
    else:
        where_parts.append("workspace_id = :wid")
        params["wid"] = workspace_id

    where_sql = " AND ".join(where_parts)
    sql = text(
        f"""
        SELECT id, kind, source_key, text, chunk_metadata,
               1 - (embedding <=> CAST(:qvec AS vector)) AS score
        FROM rag_chunks
        WHERE embedding IS NOT NULL AND {where_sql}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :k
        """
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return [
        RetrievedChunk(
            id=str(r["id"]),
            kind=r["kind"],
            source_key=r["source_key"],
            text=r["text"],
            score=float(r["score"]),
            metadata=dict(r["chunk_metadata"] or {}),
        )
        for r in rows
    ]


async def _retrieve_sqlite(
    session: AsyncSession,
    qvec: list[float],
    workspace_id: UUID | None,
    top_k: int,
    include_global: bool,
) -> list[RetrievedChunk]:
    stmt = select(RagChunk).where(RagChunk.embedding.is_not(None))
    if workspace_id is None:
        stmt = stmt.where(RagChunk.workspace_id.is_(None))
    elif include_global:
        stmt = stmt.where(
            (RagChunk.workspace_id == workspace_id)
            | (RagChunk.workspace_id.is_(None))
        )
    else:
        stmt = stmt.where(RagChunk.workspace_id == workspace_id)
    rows = (await session.execute(stmt)).scalars().all()

    scored: list[tuple[float, RagChunk]] = []
    for r in rows:
        emb = r.embedding
        if not isinstance(emb, list) or len(emb) != len(qvec):
            continue
        scored.append((_dot(qvec, emb), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]
    return [
        RetrievedChunk(
            id=str(r.id),
            kind=r.kind,
            source_key=r.source_key,
            text=r.chunk_text,
            score=float(score),
            metadata=dict(r.chunk_metadata or {}),
        )
        for score, r in top
    ]


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


__all__ = ["RetrievedChunk", "retrieve"]
