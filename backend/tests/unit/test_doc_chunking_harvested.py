"""Tests for the harvested-doc chunker."""
from __future__ import annotations

from app.services.rag.chunking import chunk_harvested_doc


def test_chunk_harvested_doc_basic() -> None:
    chunks = chunk_harvested_doc(
        "source-123", "report.pdf",
        "This is a short body.",
    )
    assert len(chunks) == 1
    c = chunks[0]
    assert c.kind == "harvested_doc"
    assert c.source_key == "docsource:source-123:report.pdf:0"
    assert "Document: report.pdf" in c.text
    assert c.metadata["source_id"] == "source-123"
    assert c.metadata["filename"] == "report.pdf"
    assert c.metadata["chunk_index"] == 0


def test_chunk_harvested_doc_splits_long_body() -> None:
    body = "x" * 5000
    chunks = chunk_harvested_doc("s", "long.txt", body)
    assert len(chunks) >= 4
    # Source-key sequence is contiguous from 0.
    indices = [c.metadata["chunk_index"] for c in chunks]
    assert indices == list(range(len(chunks)))


def test_chunk_harvested_doc_empty_body() -> None:
    assert chunk_harvested_doc("s", "empty.txt", "") == []
    assert chunk_harvested_doc("s", "empty.txt", "   \n\n   ") == []


def test_chunk_harvested_doc_extra_metadata_merged() -> None:
    chunks = chunk_harvested_doc(
        "s", "f.txt", "hi", extra_metadata={"lang": "uz", "source_url": "https://x"}
    )
    assert chunks[0].metadata["lang"] == "uz"
    assert chunks[0].metadata["source_url"] == "https://x"
    # Required keys still present.
    assert chunks[0].metadata["filename"] == "f.txt"


def test_chunk_harvested_doc_multilingual() -> None:
    """The chunker doesn't know about languages — it just splits.
    Verify Cyrillic / Latin / Uzbek-Cyrillic bytes round-trip
    intact through chunk text."""
    body = (
        "Russian: Привет, как дела?\n"
        "Uzbek-Latin: Salom, qalaysiz?\n"
        "Uzbek-Cyrillic: Салом, қалайсиз?\n"
        "English: Hi, how are you?\n"
    )
    chunks = chunk_harvested_doc("s", "greetings.txt", body)
    assert len(chunks) == 1
    text = chunks[0].text
    assert "Привет" in text
    assert "Salom" in text
    assert "Салом" in text
    assert "Hi" in text
