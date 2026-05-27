"""Tests for the citation builder."""
from __future__ import annotations

from app.services.rag.citations import (
    build_citations,
    citation_hint_for_planner,
)


def _chunk(
    *, kind: str, filename: str = "", document_id: str = "",
    source_id: str = "", title: str = "", chunk_index: int = 0,
    text: str = "", source_key: str = "",
) -> dict:
    md = {"chunk_index": chunk_index}
    if kind == "harvested_doc":
        md["filename"] = filename
        md["source_id"] = source_id
    elif kind == "user_doc":
        md["document_id"] = document_id
        md["title"] = title
    return {
        "kind": kind,
        "source_key": source_key or f"key:{kind}:{filename or document_id}",
        "text": text,
        "metadata": md,
    }


def test_empty_input_returns_empty() -> None:
    assert build_citations(None) == []
    assert build_citations([]) == []


def test_schema_chunks_filtered_out() -> None:
    chunks = [
        {"kind": "schema_table", "text": "...", "metadata": {}},
        {"kind": "schema_column", "text": "...", "metadata": {}},
        {"kind": "api_endpoint", "text": "...", "metadata": {}},
    ]
    assert build_citations(chunks) == []


def test_harvested_doc_citation() -> None:
    chunks = [
        _chunk(
            kind="harvested_doc",
            filename="hr-handbook.pdf",
            source_id="src-1",
            text="Document: hr-handbook.pdf\n\nVacation policy is 24 days per year.",
        ),
    ]
    out = build_citations(chunks)
    assert len(out) == 1
    c = out[0]
    assert c["kind"] == "harvested_doc"
    assert c["filename"] == "hr-handbook.pdf"
    assert c["source_id"] == "src-1"
    assert "Vacation policy" in c["snippet"]
    # The "Document: ..." preamble is stripped from the snippet.
    assert not c["snippet"].startswith("Document:")


def test_user_doc_citation() -> None:
    chunks = [
        _chunk(
            kind="user_doc",
            document_id="doc-42",
            title="Onboarding",
            text="Document: Onboarding\n\nFirst day checklist.",
        ),
    ]
    out = build_citations(chunks)
    assert len(out) == 1
    assert out[0]["filename"] == "Onboarding"
    assert out[0]["source_id"] == "doc-42"


def test_deduplicates_by_source() -> None:
    """Multiple chunks from the same document collapse to one citation
    (the first / highest-ranked snippet wins)."""
    chunks = [
        _chunk(
            kind="harvested_doc", filename="policy.pdf", source_id="s1",
            chunk_index=0, text="Document: policy.pdf\n\nFirst chunk text.",
        ),
        _chunk(
            kind="harvested_doc", filename="policy.pdf", source_id="s1",
            chunk_index=1, text="Document: policy.pdf\n\nSecond chunk.",
        ),
        _chunk(
            kind="harvested_doc", filename="other.pdf", source_id="s1",
            chunk_index=0, text="Document: other.pdf\n\nOther.",
        ),
    ]
    out = build_citations(chunks)
    assert len(out) == 2
    assert {c["filename"] for c in out} == {"policy.pdf", "other.pdf"}
    # First-seen snippet wins.
    policy = next(c for c in out if c["filename"] == "policy.pdf")
    assert "First chunk text" in policy["snippet"]


def test_caps_at_five_citations() -> None:
    chunks = [
        _chunk(
            kind="harvested_doc",
            filename=f"doc-{i}.pdf",
            source_id=f"s-{i}",
            text=f"Document: doc-{i}.pdf\n\nbody {i}",
        )
        for i in range(10)
    ]
    out = build_citations(chunks)
    assert len(out) == 5


def test_long_snippet_truncated() -> None:
    long_text = "Document: x.pdf\n\n" + ("word " * 200)
    chunks = [
        _chunk(
            kind="harvested_doc", filename="x.pdf", source_id="s",
            text=long_text,
        ),
    ]
    out = build_citations(chunks)
    assert out[0]["snippet"].endswith("…")
    assert len(out[0]["snippet"]) <= 281  # 280 + ellipsis


def test_multilingual_snippet_preserved() -> None:
    chunks = [
        _chunk(
            kind="harvested_doc", filename="o-bayonnoma.pdf", source_id="s",
            text="Document: o-bayonnoma.pdf\n\nКомпания внедрила новую политику возврата. Salom dunyo.",
        ),
    ]
    out = build_citations(chunks)
    assert "Компания" in out[0]["snippet"]
    assert "Salom dunyo" in out[0]["snippet"]


def test_citation_hint_empty() -> None:
    assert citation_hint_for_planner([]) == ""


def test_citation_hint_numbers_each_source() -> None:
    chunks = [
        _chunk(kind="harvested_doc", filename="a.pdf", source_id="s1"),
        _chunk(kind="harvested_doc", filename="b.pdf", source_id="s2"),
    ]
    out = build_citations(chunks)
    hint = citation_hint_for_planner(out)
    assert "[1] a.pdf" in hint
    assert "[2] b.pdf" in hint
    assert "Sources retrieved" in hint
