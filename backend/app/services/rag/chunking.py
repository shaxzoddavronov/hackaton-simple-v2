"""Convert structured sources into RAG chunks.

Three sources feed the index:
  1. A workspace's :class:`SchemaBundle` → one chunk per table (+ optional
     per-column chunk for high-cardinality columns with categorical samples).
  2. The QueryMind REST API itself → one chunk per route, so the agent can
     answer "how do I add a workspace?" type questions.
  3. User-uploaded documents → fixed-window text chunks with overlap.

Every chunk has:
  - ``kind``        : one of the model's CHECK-constrained values.
  - ``source_key``  : stable identifier (e.g. ``"public.orders"``).
  - ``text``        : the string fed to the embedding model.
  - ``metadata``    : structured info echoed back to the planner (dialect,
                      column dtypes, FK targets, HTTP method, etc.).
  - ``content_hash``: SHA-256 of ``text``; lets the indexer skip unchanged rows.

The functions here are pure — no DB I/O — so they are trivially testable.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.engines.base import SchemaBundle, TableMeta


@dataclass(slots=True)
class Chunk:
    kind: str
    source_key: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def chunk_schema_bundle(bundle: SchemaBundle) -> list[Chunk]:
    """Build chunks for every table in the bundle.

    One chunk per table, dense enough to retrieve on either table-name or
    column-name terms. Categorical samples are folded in (up to 12 values per
    column) so the planner sees that ``region IN {'EMEA','APAC',...}``.
    """
    out: list[Chunk] = []
    for t in bundle.tables:
        out.append(_table_chunk(t, bundle))
    return out


def _table_chunk(t: TableMeta, bundle: SchemaBundle) -> Chunk:
    qname = f"{t.schema}.{t.name}"
    parts: list[str] = []
    parts.append(f"Table: {qname} (dialect: {bundle.dialect})")
    if t.row_count_estimate is not None:
        parts.append(f"Approximate row count: {t.row_count_estimate}")

    col_lines: list[str] = []
    for c in t.columns:
        tags: list[str] = []
        if c.is_pk:
            tags.append("primary key")
        if c.is_unique:
            tags.append("unique")
        if c.is_id:
            tags.append("identifier")
        if not c.nullable:
            tags.append("not null")
        if c.fk_to:
            tags.append(f"fk -> {c.fk_to}")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        col_lines.append(f"  - {c.name}: {c.data_type}{tag_str}")
    if col_lines:
        parts.append("Columns:")
        parts.extend(col_lines)

    if t.foreign_keys:
        parts.append("Foreign keys:")
        for fk in t.foreign_keys:
            parts.append(
                f"  - ({', '.join(fk.from_columns)}) -> "
                f"{fk.to_table} ({', '.join(fk.to_columns)})"
            )

    # Categorical samples (cap to 12 to keep chunks compact).
    cols_samples = bundle.samples.get(qname, {}) if bundle.samples else {}
    sample_lines: list[str] = []
    for cname, s in cols_samples.items():
        # `s` may be a ColumnSample model or a dict (DB round-trip uses dicts).
        distinct = _get(s, "distinct_values")
        truncated = bool(_get(s, "distinct_truncated"))
        if distinct:
            vals = ", ".join(repr(v) for v in list(distinct)[:12])
            tail = ", ..." if truncated else ""
            sample_lines.append(f"  - {cname} in {{ {vals}{tail} }}")
    if sample_lines:
        parts.append("Sample categorical values:")
        parts.extend(sample_lines)

    text = "\n".join(parts)
    metadata = {
        "dialect": bundle.dialect,
        "schema": t.schema,
        "table": t.name,
        "columns": [
            {"name": c.name, "type": c.data_type, "is_id": c.is_id}
            for c in t.columns
        ],
        "fks": [
            {
                "from": fk.from_columns,
                "to_table": fk.to_table,
                "to": fk.to_columns,
            }
            for fk in t.foreign_keys
        ],
    }
    return Chunk(
        kind="schema_table",
        source_key=qname,
        text=text,
        metadata=metadata,
    )


def chunk_api_endpoints(routes: Iterable[dict[str, Any]]) -> list[Chunk]:
    """Build chunks for QueryMind's own REST API.

    ``routes`` is the shape produced by :func:`api_catalog.iter_routes` —
    each entry has ``method``, ``path``, ``summary``, ``description``.
    """
    out: list[Chunk] = []
    for r in routes:
        method = str(r.get("method", "GET")).upper()
        path = str(r.get("path", ""))
        summary = (r.get("summary") or "").strip()
        description = (r.get("description") or "").strip()
        body_parts = [f"API: {method} {path}"]
        if summary:
            body_parts.append(summary)
        if description and description != summary:
            body_parts.append(description)
        text = "\n".join(body_parts)
        out.append(
            Chunk(
                kind="api_endpoint",
                source_key=f"{method} {path}",
                text=text,
                metadata={"method": method, "path": path},
            )
        )
    return out


# Document chunking — fixed character window with overlap. Markdown-aware
# splitting (on headers) is intentionally deferred until the upload UI exists.
_DOC_CHUNK_SIZE = 1200
_DOC_CHUNK_OVERLAP = 200


def chunk_document(
    document_id: str,
    title: str,
    body: str,
    *,
    chunk_size: int = _DOC_CHUNK_SIZE,
    overlap: int = _DOC_CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split a long document into overlapping chunks.

    Each chunk's ``source_key`` is ``"{document_id}:{index}"``.
    """
    body = body.strip()
    if not body:
        return []
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("invalid chunking parameters")

    chunks: list[Chunk] = []
    step = chunk_size - overlap
    i = 0
    idx = 0
    while i < len(body):
        slice_ = body[i : i + chunk_size]
        text = f"Document: {title}\n\n{slice_}"
        chunks.append(
            Chunk(
                kind="user_doc",
                source_key=f"{document_id}:{idx}",
                text=text,
                metadata={
                    "document_id": document_id,
                    "title": title,
                    "chunk_index": idx,
                },
            )
        )
        i += step
        idx += 1
    return chunks


def chunk_harvested_doc(
    source_id: str,
    filename: str,
    text: str,
    *,
    chunk_size: int = _DOC_CHUNK_SIZE,
    overlap: int = _DOC_CHUNK_OVERLAP,
    extra_metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """Like :func:`chunk_document` but tagged for harvested sources.

    ``source_key`` is ``"docsource:<source_id>:<filename>:<idx>"`` so
    re-harvesting the same source overwrites stable keys (chunker is
    deterministic) and orphan-deletion can scope by source_id.
    """
    body = (text or "").strip()
    if not body:
        return []
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("invalid chunking parameters")

    chunks: list[Chunk] = []
    step = chunk_size - overlap
    i = 0
    idx = 0
    while i < len(body):
        slice_ = body[i : i + chunk_size]
        chunk_text_value = f"Document: {filename}\n\n{slice_}"
        metadata: dict[str, Any] = {
            "source_id": source_id,
            "filename": filename,
            "chunk_index": idx,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        chunks.append(
            Chunk(
                kind="harvested_doc",
                source_key=f"docsource:{source_id}:{filename}:{idx}",
                text=chunk_text_value,
                metadata=metadata,
            )
        )
        i += step
        idx += 1
    return chunks


def _get(s: Any, key: str) -> Any:
    if isinstance(s, dict):
        return s.get(key)
    return getattr(s, key, None)


__all__ = [
    "Chunk",
    "chunk_schema_bundle",
    "chunk_api_endpoints",
    "chunk_document",
    "chunk_harvested_doc",
]
