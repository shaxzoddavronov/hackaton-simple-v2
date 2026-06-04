"""Phase 38 — question similarity recall.

After every successful chat turn the API persists a ``qa_history``
row in ``rag_chunks`` carrying (user question + assistant headline)
embedded via Triton. Before the agent runs on the next turn, the
chat path semantic-searches this sub-index; high-similarity hits
are streamed to the frontend so the user gets a "you asked this
before" chip with a one-click re-run.

Two-function surface:

  * :func:`index_qa_pair` — INSERT a fresh qa_history row at turn-end.
  * :func:`find_similar`  — SELECT top-K matches above a cosine
    threshold for the current turn's user question.

Failure mode: every Triton call is wrapped — a Triton outage
must NEVER break the chat path. Indexing and search both
short-circuit to no-ops if the embed fails.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.rag.triton_client import get_client

log = logging.getLogger(__name__)


# A hit needs to be this close on cosine to qualify as "you asked
# this before". Lower → more false-positive chips, higher → fewer
# but more confident. 0.85 picked empirically from the bge-m3
# multilingual smoke tests.
SIMILARITY_THRESHOLD = 0.85
DEFAULT_TOP_K = 3
# Don't bother indexing trivially short prompts ("ok", "what?") —
# they generate noise in the recall search and the cost is the
# same as a real embed call. The headline is allowed to be short
# (single KPI label) so we gate on the QUESTION only.
MIN_QUESTION_LEN = 6


@dataclass(slots=True)
class QaHit:
    """One matched past Q-A pair surfaced to the user."""
    message_id: str
    session_id: str
    question: str
    headline: str
    similarity: float


async def index_qa_pair(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    message_id: UUID,
    session_id: UUID,
    question: str,
    headline: str,
) -> None:
    """Embed (question + headline) and INSERT into rag_chunks with
    kind='qa_history'. Called from api/chat.py AFTER a successful
    assistant turn lands."""
    q = (question or "").strip()
    if len(q) < MIN_QUESTION_LEN:
        return
    h = (headline or "").strip()
    text = q if not h else f"{q}\n\nAnswer: {h}"

    try:
        client = get_client()
        embed = await client.embed([text])
        vector = embed.vectors[0]
    except Exception as e:  # noqa: BLE001
        log.warning(
            "qa_history.index_qa_pair: Triton failed (%s); skipping", e
        )
        return

    metadata = {
        "question": q[:500],
        "headline": h[:500],
        "message_id": str(message_id),
        "session_id": str(session_id),
    }
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # source_key encodes the message so an accidental re-insert
    # would violate the (workspace_id, kind, source_key) unique
    # index — exactly what we want.
    source_key = f"qa::{message_id}"

    try:
        await session.execute(
            sa_text(
                "INSERT INTO rag_chunks ("
                "  workspace_id, connection_id, document_id, "
                "  kind, source_key, text, embedding, "
                "  chunk_metadata, content_hash"
                ") VALUES ("
                "  :workspace_id, NULL, NULL, "
                "  'qa_history', :source_key, :text, "
                "  CAST(:embedding AS vector), "
                "  CAST(:metadata AS jsonb), :content_hash"
                ") ON CONFLICT (workspace_id, kind, source_key) "
                "DO NOTHING"
            ),
            {
                "workspace_id": workspace_id,
                "source_key": source_key,
                "text": text[:4000],
                "embedding": _format_vector(vector),
                "metadata": json.dumps(metadata),
                "content_hash": content_hash,
            },
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        log.exception("qa_history.index_qa_pair: insert failed")
        try:
            await session.rollback()
        except Exception:  # pragma: no cover
            pass


async def find_similar(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    question: str,
    threshold: float = SIMILARITY_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
    exclude_message_id: UUID | None = None,
) -> list[QaHit]:
    """Return the top past Q-A pairs whose embedding is at least
    ``threshold`` close on cosine to the new question. Empty list
    when Triton is unavailable or no rows meet the bar."""
    q = (question or "").strip()
    if len(q) < MIN_QUESTION_LEN:
        return []
    try:
        client = get_client()
        embed = await client.embed([q])
        qvec = embed.vectors[0]
    except Exception as e:  # noqa: BLE001
        log.warning(
            "qa_history.find_similar: Triton failed (%s); empty hits", e
        )
        return []

    # cosine similarity = 1 - cosine distance (<=>). pgvector's <=>
    # returns the cosine distance for normalised vectors. bge-m3
    # output is already L2-normalised so dot product == cosine.
    #
    # Build the exclude clause conditionally. Earlier we used a
    # `(:exclude IS NULL OR source_key <> :exclude_key)` pattern
    # but asyncpg's prepared-statement protocol bails with
    # `IndeterminateDatatypeError: could not determine data type
    # of parameter $3` when :exclude binds to Python None — the
    # `IS NULL` operator works on any type so the planner has no
    # other clue. Splitting the SQL keeps each bind unambiguous.
    params: dict[str, object] = {
        "qvec": _format_vector(qvec),
        "workspace_id": workspace_id,
        "k": int(top_k),
    }
    exclude_clause = ""
    if exclude_message_id is not None:
        exclude_clause = "AND source_key <> :exclude_key"
        params["exclude_key"] = f"qa::{exclude_message_id}"

    rows = await session.execute(
        sa_text(
            f"""
            SELECT chunk_metadata,
                   1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
            FROM rag_chunks
            WHERE workspace_id = :workspace_id
              AND kind = 'qa_history'
              {exclude_clause}
            ORDER BY embedding <=> CAST(:qvec AS vector)
            LIMIT :k
            """
        ),
        params,
    )

    hits: list[QaHit] = []
    for raw in rows.mappings():
        sim = float(raw["similarity"] or 0.0)
        if sim < threshold:
            continue
        md = raw["chunk_metadata"] or {}
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except (ValueError, TypeError):
                md = {}
        if not isinstance(md, dict):
            continue
        hits.append(
            QaHit(
                message_id=str(md.get("message_id") or ""),
                session_id=str(md.get("session_id") or ""),
                question=str(md.get("question") or ""),
                headline=str(md.get("headline") or ""),
                similarity=round(sim, 4),
            )
        )
    return hits


def _format_vector(vec: list[float]) -> str:
    """pgvector accepts the bracketed comma-separated literal."""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


__all__ = [
    "DEFAULT_TOP_K",
    "MIN_QUESTION_LEN",
    "QaHit",
    "SIMILARITY_THRESHOLD",
    "find_similar",
    "index_qa_pair",
]
