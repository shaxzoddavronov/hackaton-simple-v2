"""Build a compact list of citations from the retriever's chunks.

The agent's RAG retriever returns a stream of chunks (schema +
api_endpoint + user_doc + harvested_doc). Only the document-shaped
kinds make useful citations — users care about "which PDF answered
this question", not "we looked at the orders table". This module
filters + deduplicates + truncates them into a UI-friendly payload.

Output shape — list of dicts (kept loose since the frontend just
needs to render them; introducing a Pydantic class would force a UI-
spec migration with no compatibility win):

    {
      "kind":        "harvested_doc" | "user_doc",
      "source_id":   str,             # docsource:<id> or document:<id>
      "filename":    str,             # human-readable label
      "snippet":     str,             # up to ~280 chars of body text
      "chunk_index": int,
      "source_key":  str,             # raw RagChunk source_key for re-fetch
    }

Citations are ordered by retriever rank (the retriever returns chunks
sorted by similarity already). Duplicate filenames are collapsed —
we keep the highest-ranked snippet per source.
"""
from __future__ import annotations

from typing import Any

_MAX_CITATIONS = 5
_SNIPPET_CHARS = 280


def build_citations(
    retrieved: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Filter + dedupe retrieved chunks into a citation list.

    ``retrieved`` is the shape :mod:`services.rag.retriever` returns
    (a list of dicts each with at least ``kind``, ``source_key``,
    ``text``, ``metadata``). The function tolerates missing fields so
    a fragile retriever can't crash the SSE final event.
    """
    if not retrieved:
        return []

    seen_keys: set[str] = set()
    out: list[dict[str, Any]] = []

    for chunk in retrieved:
        kind = str(chunk.get("kind") or "")
        if kind not in ("harvested_doc", "user_doc"):
            continue

        metadata = chunk.get("metadata") or {}
        text = str(chunk.get("text") or "")
        source_key = str(chunk.get("source_key") or "")

        if kind == "harvested_doc":
            filename = str(metadata.get("filename") or "")
            source_id = str(metadata.get("source_id") or "")
            display = filename or source_key
            dedupe_key = f"harvested:{source_id}:{filename}"
        else:
            doc_id = str(metadata.get("document_id") or "")
            title = str(metadata.get("title") or "")
            display = title or doc_id or source_key
            dedupe_key = f"doc:{doc_id}"

        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        out.append(
            {
                "kind": kind,
                "source_id": (
                    str(metadata.get("source_id") or "")
                    if kind == "harvested_doc"
                    else str(metadata.get("document_id") or "")
                ),
                "filename": display,
                "snippet": _snippet(text),
                "chunk_index": int(metadata.get("chunk_index") or 0),
                "source_key": source_key,
            }
        )
        if len(out) >= _MAX_CITATIONS:
            break

    return out


def citation_hint_for_planner(citations: list[dict[str, Any]]) -> str:
    """Compact one-liner per citation that the answer_writer can paste
    into its prompt so the LLM can reference the sources naturally.

    Kept terse — the model gets the full chunk text in the
    ``retrieved_chunks`` block already; this list is just a labeled
    table-of-contents the answer can cite by name.
    """
    if not citations:
        return ""
    lines = [
        f"  [{i + 1}] {c['filename']}"
        for i, c in enumerate(citations)
    ]
    return "Sources retrieved (cite by [number] in body_md):\n" + "\n".join(lines)


def _snippet(text: str) -> str:
    """Pull a short, single-paragraph preview from a chunk."""
    s = text.strip()
    if not s:
        return ""
    # Drop the leading "Document: <title>" header that chunk_document
    # prepends so the snippet shows real content, not metadata.
    if s.startswith("Document: "):
        # Skip the title line + the blank line after it.
        nl = s.find("\n\n")
        if nl >= 0:
            s = s[nl + 2 :]
    s = s.replace("\r", "")
    # Single space runs; preserve paragraph breaks as one newline.
    s = " ".join(s.split())
    if len(s) > _SNIPPET_CHARS:
        s = s[: _SNIPPET_CHARS - 1] + "…"
    return s


__all__ = ["build_citations", "citation_hint_for_planner"]
