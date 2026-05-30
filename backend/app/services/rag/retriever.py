"""Hybrid top-K retrieval over ``rag_chunks``.

Phase 25 — combines dense (bge-m3 via Triton + pgvector) AND lexical
(BM25) retrieval with reciprocal-rank-fusion. Dense alone misses
exact-term hits: a question like "what does error ESF-4421 mean?"
embeds to a similar vector regardless of which specific code is
mentioned, so it ranks chunks about errors-in-general above the
one chunk that literally contains "ESF-4421". BM25 catches those.

Fusion shape — reciprocal rank fusion (RRF), the simplest robust
hybrid scheme. For each candidate chunk we sum
``1 / (k + rank_dense) + 1 / (k + rank_bm25)`` with k=60, then sort
desc and take top_k. No score normalisation needed (RRF is rank-
based) and chunks that only appear in one list still rank ahead of
ones that appear in neither.

Postgres path: pgvector dense + Postgres tsvector BM25-ish via
``ts_rank_cd`` (close enough — full BM25 would require pgsearch or
a custom extension). Workspace-scoped filtering happens in SQL.

SQLite path: pulls all matching chunks and runs both cosine + a
pure-Python BM25 in memory. Only used by tests + tiny demo DBs.

If Triton is unreachable we **don't crash** — dense returns empty,
BM25 still runs, and the union becomes the returned list. The
agent stays functional during Triton restarts.
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


RRF_K = 60  # standard RRF constant; raise to flatten the curve

# How many candidates to pull from each retriever before fusion.
# We over-fetch so the fusion has signal even when one list misses
# the right chunk entirely.
_PER_LIST_K = 30


async def retrieve(
    session: AsyncSession,
    *,
    query: str,
    workspace_id: UUID | None,
    top_k: int = 12,
    include_global: bool = True,
) -> list[RetrievedChunk]:
    """Hybrid top-K (dense + BM25, RRF-fused). Empty query → empty
    list. Triton outage → BM25 alone. Both retrievers run scoped to
    the workspace (and optionally global chunks).
    """
    if not query.strip():
        return []

    # Dense retrieval — gated by Triton availability.
    dense: list[RetrievedChunk] = []
    client = get_client()
    if client.enabled:
        try:
            resp = await client.embed([query])
            if resp.vectors:
                qvec = resp.vectors[0]
                if _is_postgres(session):
                    dense = await _retrieve_pgvector(
                        session, qvec, workspace_id,
                        _PER_LIST_K, include_global,
                    )
                else:
                    dense = await _retrieve_sqlite(
                        session, qvec, workspace_id,
                        _PER_LIST_K, include_global,
                    )
        except (TritonUnavailable, TritonError) as e:
            log.warning(
                "RAG dense retrieval failed (%s); falling back to BM25 only",
                e,
            )

    # BM25 lexical retrieval — always runs (no external dep beyond
    # the DB itself).
    if _is_postgres(session):
        bm25 = await _retrieve_bm25_postgres(
            session, query, workspace_id, _PER_LIST_K, include_global
        )
    else:
        bm25 = await _retrieve_bm25_sqlite(
            session, query, workspace_id, _PER_LIST_K, include_global
        )

    return _fuse_rrf(dense, bm25, top_k=top_k)


def _fuse_rrf(
    dense: list[RetrievedChunk],
    bm25: list[RetrievedChunk],
    *,
    top_k: int,
) -> list[RetrievedChunk]:
    """Reciprocal-rank fusion. Score for a chunk seen at rank r in
    a list contributes ``1 / (RRF_K + r)``. Chunks that appear in
    both lists naturally rise to the top; chunks in only one list
    still get scored.

    Returns a list of RetrievedChunk with the fused score replacing
    the per-retriever score, sorted desc.
    """
    fused: dict[str, RetrievedChunk] = {}
    scores: dict[str, float] = {}
    for rank, chunk in enumerate(dense):
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (RRF_K + rank + 1)
        fused.setdefault(chunk.id, chunk)
    for rank, chunk in enumerate(bm25):
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (RRF_K + rank + 1)
        fused.setdefault(chunk.id, chunk)
    ordered = sorted(fused.values(), key=lambda c: scores[c.id], reverse=True)
    out: list[RetrievedChunk] = []
    for c in ordered[:top_k]:
        # Replace the raw per-retriever score with the fused one so
        # downstream code sees a consistent ranking signal.
        out.append(
            RetrievedChunk(
                id=c.id,
                kind=c.kind,
                source_key=c.source_key,
                text=c.text,
                score=float(scores[c.id]),
                metadata=c.metadata,
            )
        )
    return out


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


async def _retrieve_bm25_postgres(
    session: AsyncSession,
    query: str,
    workspace_id: UUID | None,
    top_k: int,
    include_global: bool,
) -> list[RetrievedChunk]:
    """Lexical retrieval via Postgres full-text search.

    Uses ``websearch_to_tsquery`` so the user can write queries like
    ``"refund policy" -archived`` without learning tsquery syntax.
    ``ts_rank_cd`` ranks matches by frequency + position; we sort
    desc and return top_k. Identical workspace-scope predicates as
    the dense path.

    Postgres tsvectors aren't BM25 in the strict sense — they're
    ts_rank_cd — but the ranking properties are close enough for
    RRF fusion: a chunk with the exact phrase ranks ahead of one
    with scattered terms, which is the signal RRF needs to lift
    exact-term hits above the dense embedder's general topicality.
    """
    where_parts: list[str] = []
    params: dict[str, Any] = {"q": query, "k": top_k}
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
               ts_rank_cd(
                 to_tsvector('simple', text),
                 websearch_to_tsquery('simple', :q)
               ) AS score
        FROM rag_chunks
        WHERE {where_sql}
          AND to_tsvector('simple', text)
              @@ websearch_to_tsquery('simple', :q)
        ORDER BY score DESC
        LIMIT :k
        """
    )
    try:
        rows = (await session.execute(sql, params)).mappings().all()
    except Exception as e:
        # Bad tsquery syntax shouldn't kill the retrieval — log and
        # return empty so dense still answers.
        log.warning("RAG BM25 (postgres) failed: %s", e)
        return []
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


async def _retrieve_bm25_sqlite(
    session: AsyncSession,
    query: str,
    workspace_id: UUID | None,
    top_k: int,
    include_global: bool,
) -> list[RetrievedChunk]:
    """In-memory BM25 over the workspace's chunks. Only used by
    unit tests and tiny demo databases — production runs Postgres.

    Implementation: tokenise on whitespace / punctuation,
    lower-case, compute classical BM25 (k1=1.5, b=0.75) over the
    workspace's chunk corpus. No stemming, no stop-word filter —
    bge-m3 covers semantic equivalence; BM25's role is exact-term
    catching.
    """
    stmt = select(RagChunk)
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
    if not rows:
        return []

    q_terms = _tokenise(query)
    if not q_terms:
        return []

    # Build corpus stats.
    docs: list[list[str]] = [_tokenise(r.chunk_text) for r in rows]
    doc_lens = [len(d) for d in docs]
    avgdl = sum(doc_lens) / max(1, len(doc_lens))
    n_docs = len(docs)

    # IDF per query term.
    import math

    df: dict[str, int] = {}
    for term in set(q_terms):
        df[term] = sum(1 for d in docs if term in d)
    idf = {
        term: math.log(
            (n_docs - df_t + 0.5) / (df_t + 0.5) + 1.0
        )
        for term, df_t in df.items()
    }
    k1, b = 1.5, 0.75

    scored: list[tuple[float, RagChunk]] = []
    for r, doc, dl in zip(rows, docs, doc_lens):
        if not doc:
            continue
        score = 0.0
        tf: dict[str, int] = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1
        for term in q_terms:
            if term not in tf:
                continue
            f = tf[term]
            score += idf.get(term, 0.0) * (
                f * (k1 + 1)
                / (f + k1 * (1 - b + b * dl / max(1.0, avgdl)))
            )
        if score > 0:
            scored.append((score, r))
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


def _tokenise(s: str) -> list[str]:
    """Lower-case + split on whitespace / punctuation. Keeps unicode
    letters (Cyrillic, etc.) so cross-lingual queries work."""
    import re

    return [
        t for t in re.split(r"[\W_]+", (s or "").lower(), flags=re.UNICODE)
        if t
    ]


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


__all__ = ["RetrievedChunk", "retrieve"]
