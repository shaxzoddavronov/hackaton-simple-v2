"""Embed chunks via Triton, upsert into the ``rag_chunks`` table.

This module is the only writer to ``rag_chunks``. Three public entrypoints:

  - :func:`reindex_workspace` — re-embed all schema chunks for one workspace
    (deletes orphaned rows first so removed tables go away).
  - :func:`reindex_api_catalog` — re-embed the QueryMind REST routes
    (``workspace_id = NULL``).
  - :func:`reindex_document` — re-embed an uploaded document.

A ``content_hash`` short-circuit means already-fresh chunks skip the Triton
round-trip. Hashes are matched per ``(workspace_id, kind, source_key)``.

Storage choice is dialect-aware: on Postgres the ``embedding`` column is
``vector(1024)`` (pgvector) and we send the literal ``'[0.12,0.34,...]'``
text representation, which pgvector parses. On SQLite the column is a
JSON column and we just store a Python list of floats.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    RagChunk,
    SchemaBundle as SchemaBundleRow,
    UploadedDocument,
    WorkspaceConnection,
)
from app.engines.base import (
    ColumnMeta,
    ForeignKeyMeta,
    SchemaBundle as SchemaBundleDto,
    TableMeta,
)
from app.services.rag.api_catalog import iter_routes
from app.services.rag.chunking import (
    Chunk,
    chunk_api_endpoints,
    chunk_document,
    chunk_schema_bundle,
)
from app.services.rag.triton_client import (
    TritonUnavailable,
    get_client,
)

log = logging.getLogger(__name__)


async def reindex_connection(
    session: AsyncSession,
    connection_id: UUID,
) -> dict[str, int]:
    """Full reindex of one connection's schema chunks.

    Chunks are tagged with the parent workspace_id (for workspace-level
    retrieval) and connection_id (for per-DB retrieval). Returns a small
    report: ``{"upserted": N, "skipped": M, "removed": K}``.
    """
    conn = await session.get(WorkspaceConnection, connection_id)
    if conn is None:
        log.warning("reindex_connection: connection %s not found", connection_id)
        return {"upserted": 0, "skipped": 0, "removed": 0}

    bundle = await _load_bundle(session, connection_id)
    if bundle is None:
        log.warning(
            "reindex_connection: connection %s has no schema bundle", connection_id
        )
        return {"upserted": 0, "skipped": 0, "removed": 0}

    chunks = chunk_schema_bundle(bundle)
    report = await _upsert_chunks(
        session,
        workspace_id=conn.workspace_id,
        connection_id=connection_id,
        document_id=None,
        chunks=chunks,
        prune_kinds=("schema_table", "schema_column"),
    )
    await session.commit()
    return report


async def reindex_api_catalog(session: AsyncSession) -> dict[str, int]:
    """Reindex the QueryMind REST API routes (global; ``workspace_id NULL``)."""
    chunks = chunk_api_endpoints(iter_routes())
    report = await _upsert_chunks(
        session,
        workspace_id=None,
        document_id=None,
        chunks=chunks,
        prune_kinds=("api_endpoint",),
    )
    await session.commit()
    return report


async def reindex_document(
    session: AsyncSession,
    document_id: UUID,
) -> dict[str, int]:
    """Re-chunk + re-embed a single uploaded document."""
    doc = await session.get(UploadedDocument, document_id)
    if doc is None:
        return {"upserted": 0, "skipped": 0, "removed": 0}
    chunks = chunk_document(str(doc.id), doc.title, doc.body)
    report = await _upsert_chunks_for_document(
        session, document_id=doc.id, workspace_id=doc.workspace_id, chunks=chunks
    )
    await session.commit()
    return report


# --- internals -------------------------------------------------------------


async def _load_bundle(
    session: AsyncSession, connection_id: UUID
) -> SchemaBundleDto | None:
    row = await session.execute(
        select(SchemaBundleRow).where(SchemaBundleRow.connection_id == connection_id)
    )
    bundle_row = row.scalar_one_or_none()
    if bundle_row is None:
        return None
    return _bundle_from_row(bundle_row.bundle)


def _bundle_from_row(raw: Any) -> SchemaBundleDto:
    if isinstance(raw, str):
        raw = json.loads(raw)
    tables = []
    for t in raw.get("tables", []):
        cols = [ColumnMeta(**c) for c in t.get("columns", [])]
        fks = [ForeignKeyMeta(**fk) for fk in t.get("foreign_keys", [])]
        tables.append(
            TableMeta(
                schema=t.get("schema", "public"),
                name=t["name"],
                columns=cols,
                foreign_keys=fks,
                row_count_estimate=t.get("row_count_estimate"),
            )
        )
    return SchemaBundleDto(
        dialect=raw["dialect"], tables=tables, samples=raw.get("samples", {}) or {}
    )


async def _upsert_chunks(
    session: AsyncSession,
    *,
    workspace_id: UUID | None,
    connection_id: UUID | None = None,
    document_id: UUID | None,
    chunks: Sequence[Chunk],
    prune_kinds: tuple[str, ...],
) -> dict[str, int]:
    """Embed + upsert; delete rows in ``prune_kinds`` no longer present.

    The skip-on-hash-match optimization saves Triton round-trips on routine
    daily refreshes where nothing changed.
    """
    # Load existing hashes so we can short-circuit unchanged chunks.
    # Connection-scoped reindex narrows by connection_id so two
    # connections in the same workspace don't clobber each other.
    existing = await _load_existing(
        session,
        workspace_id=workspace_id,
        connection_id=connection_id,
        kinds=prune_kinds,
    )
    incoming_keys = {(c.kind, c.source_key) for c in chunks}

    to_embed: list[Chunk] = []
    skipped = 0
    for c in chunks:
        prev = existing.get((c.kind, c.source_key))
        if prev is not None and prev["content_hash"] == c.content_hash:
            skipped += 1
            continue
        to_embed.append(c)

    vectors = await _embed_batched(to_embed)

    upserted = 0
    for c, vec in zip(to_embed, vectors):
        prev = existing.get((c.kind, c.source_key))
        emb_storage = _embedding_for_storage(session, vec)
        if prev is None:
            row = RagChunk(
                workspace_id=workspace_id,
                connection_id=connection_id,
                document_id=document_id,
                kind=c.kind,
                source_key=c.source_key,
                chunk_text=c.text,
                embedding=emb_storage,
                chunk_metadata=c.metadata,
                content_hash=c.content_hash,
            )
            session.add(row)
        else:
            await session.execute(
                text(
                    "UPDATE rag_chunks SET text=:t, embedding=:e, "
                    "chunk_metadata=:m, content_hash=:h "
                    "WHERE id=:id"
                ).bindparams(
                    bindparam("e", type_=_embedding_bind_type(session))
                ),
                {
                    "t": c.text,
                    "e": emb_storage,
                    "m": _jsonify(session, c.metadata),
                    "h": c.content_hash,
                    "id": prev["id"],
                },
            )
        upserted += 1

    # Drop rows that disappeared from the source-of-truth (e.g., dropped table).
    removed = await _delete_orphans(
        session,
        workspace_id=workspace_id,
        connection_id=connection_id,
        kinds=prune_kinds,
        keep_keys=incoming_keys,
    )

    return {"upserted": upserted, "skipped": skipped, "removed": removed}


async def _upsert_chunks_for_document(
    session: AsyncSession,
    *,
    document_id: UUID,
    workspace_id: UUID | None,
    chunks: Sequence[Chunk],
) -> dict[str, int]:
    """Document path: scope pruning by ``document_id`` instead of kind."""
    # Wipe-and-reload: documents are rare, small, and chunking is offset-
    # sensitive (re-uploading shifts every subsequent chunk), so a clean
    # rewrite is more honest than diffing.
    await session.execute(
        delete(RagChunk).where(RagChunk.document_id == document_id)
    )
    vectors = await _embed_batched(chunks)
    for c, vec in zip(chunks, vectors):
        session.add(
            RagChunk(
                workspace_id=workspace_id,
                document_id=document_id,
                kind=c.kind,
                source_key=c.source_key,
                chunk_text=c.text,
                embedding=_embedding_for_storage(session, vec),
                chunk_metadata=c.metadata,
                content_hash=c.content_hash,
            )
        )
    return {"upserted": len(chunks), "skipped": 0, "removed": 0}


async def _load_existing(
    session: AsyncSession,
    *,
    workspace_id: UUID | None,
    connection_id: UUID | None = None,
    kinds: tuple[str, ...],
) -> dict[tuple[str, str], dict[str, Any]]:
    stmt = select(
        RagChunk.id, RagChunk.kind, RagChunk.source_key, RagChunk.content_hash
    ).where(RagChunk.kind.in_(kinds))
    if connection_id is not None:
        # Connection-scoped reindex: tightest filter.
        stmt = stmt.where(RagChunk.connection_id == connection_id)
    elif workspace_id is None:
        stmt = stmt.where(RagChunk.workspace_id.is_(None))
    else:
        stmt = stmt.where(RagChunk.workspace_id == workspace_id)
    rows = (await session.execute(stmt)).all()
    return {
        (r.kind, r.source_key): {"id": r.id, "content_hash": r.content_hash}
        for r in rows
    }


async def _delete_orphans(
    session: AsyncSession,
    *,
    workspace_id: UUID | None,
    connection_id: UUID | None = None,
    kinds: tuple[str, ...],
    keep_keys: set[tuple[str, str]],
) -> int:
    stmt = select(RagChunk.id, RagChunk.kind, RagChunk.source_key).where(
        RagChunk.kind.in_(kinds)
    )
    if connection_id is not None:
        stmt = stmt.where(RagChunk.connection_id == connection_id)
    elif workspace_id is None:
        stmt = stmt.where(RagChunk.workspace_id.is_(None))
    else:
        stmt = stmt.where(RagChunk.workspace_id == workspace_id)
    rows = (await session.execute(stmt)).all()
    orphans = [r.id for r in rows if (r.kind, r.source_key) not in keep_keys]
    if not orphans:
        return 0
    await session.execute(delete(RagChunk).where(RagChunk.id.in_(orphans)))
    return len(orphans)


async def _embed_batched(chunks: Sequence[Chunk]) -> list[list[float]]:
    if not chunks:
        return []
    client = get_client()
    if not client.enabled:
        raise TritonUnavailable("TRITON_URL is empty — cannot index without embeddings")
    batch = settings.RAG_INDEX_BATCH
    out: list[list[float]] = []
    for i in range(0, len(chunks), batch):
        slice_ = chunks[i : i + batch]
        resp = await client.embed([c.text for c in slice_])
        out.extend(resp.vectors)
    return out


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


def _embedding_for_storage(session: AsyncSession, vec: list[float]) -> Any:
    """On Postgres pgvector accepts ``'[0.1, 0.2, ...]'`` text; on SQLite we
    store a JSON list."""
    if _is_postgres(session):
        return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"
    return list(vec)


def _embedding_bind_type(session: AsyncSession):
    # Use a string bind on Postgres so the pgvector text-cast applies; let
    # SQLAlchemy infer the JSON type on SQLite (return ``None`` means default).
    from sqlalchemy import String

    if _is_postgres(session):
        return String()
    return None


def _jsonify(session: AsyncSession, m: dict[str, Any]) -> Any:
    """JSON columns are bound natively by SQLAlchemy; nothing fancy needed."""
    return m


__all__ = [
    "reindex_connection",
    "reindex_api_catalog",
    "reindex_document",
]
