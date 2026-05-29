"""Phase 17.1 — DB-column harvester yields row context that links
each chunk back to its source DB row.

Covers:
  * ``_pk_columns_from_bundle`` extracts the right PK column(s) from
    a stored SchemaBundle JSON.
  * ``_serialize_cell`` coerces non-JSON-safe values.
  * The harvested-doc chunker preserves db_row metadata through to
    chunk_metadata so the citation builder can surface it.
  * ``build_citations`` and ``citation_hint_for_planner`` render the
    row linkage in the LLM prompt + UI payload.

We don't exercise the full SQL roundtrip in this test (that needs a
live engine + a SchemaBundle row in the metadata DB). The
integration path is verified end-to-end through the existing
e2e_postgres fixture; here we lock in the pure-Python contracts.
"""
from __future__ import annotations

from app.services.doc_harvest import _pk_columns_from_bundle, _serialize_cell
from app.services.rag.chunking import chunk_harvested_doc
from app.services.rag.citations import (
    build_citations,
    citation_hint_for_planner,
)


# ── PK discovery ─────────────────────────────────────────────────


def test_pk_columns_from_bundle_finds_single_pk() -> None:
    bundle = {
        "dialect": "postgres",
        "tables": [
            {
                "name": "tickets",
                "columns": [
                    {"name": "id", "data_type": "bigint", "is_pk": True},
                    {"name": "attachment_url", "data_type": "text", "is_pk": False},
                    {"name": "title", "data_type": "text"},
                ],
            }
        ],
    }
    assert _pk_columns_from_bundle(bundle, "tickets") == ["id"]


def test_pk_columns_from_bundle_finds_composite_pk() -> None:
    bundle = {
        "tables": [
            {
                "name": "user_files",
                "columns": [
                    {"name": "user_id", "is_pk": True},
                    {"name": "file_id", "is_pk": True},
                    {"name": "url"},
                ],
            }
        ],
    }
    assert _pk_columns_from_bundle(bundle, "user_files") == [
        "user_id",
        "file_id",
    ]


def test_pk_columns_from_bundle_empty_when_table_missing() -> None:
    bundle = {"tables": [{"name": "other", "columns": []}]}
    assert _pk_columns_from_bundle(bundle, "tickets") == []


def test_pk_columns_from_bundle_empty_when_no_pk_columns() -> None:
    bundle = {
        "tables": [
            {
                "name": "events",
                "columns": [{"name": "ts"}, {"name": "payload"}],
            }
        ],
    }
    assert _pk_columns_from_bundle(bundle, "events") == []


def test_pk_columns_from_bundle_parses_json_string() -> None:
    import json

    bundle_str = json.dumps(
        {"tables": [{"name": "t", "columns": [{"name": "k", "is_pk": True}]}]}
    )
    assert _pk_columns_from_bundle(bundle_str, "t") == ["k"]


def test_pk_columns_from_bundle_tolerates_garbage() -> None:
    assert _pk_columns_from_bundle("not-json", "t") == []
    assert _pk_columns_from_bundle(None, "t") == []
    assert _pk_columns_from_bundle({"tables": "wrong-type"}, "t") == []


# ── Cell serialisation ───────────────────────────────────────────


def test_serialize_cell_keeps_json_safe_values() -> None:
    assert _serialize_cell(None) is None
    assert _serialize_cell(42) == 42
    assert _serialize_cell(3.14) == 3.14
    assert _serialize_cell(True) is True
    assert _serialize_cell("hello") == "hello"


def test_serialize_cell_strs_complex_values() -> None:
    from datetime import datetime
    from uuid import UUID

    dt = datetime(2026, 5, 28, 12, 30)
    assert _serialize_cell(dt) == str(dt)
    uid = UUID("12345678-1234-5678-1234-567812345678")
    assert _serialize_cell(uid) == str(uid)


# ── Chunker passes db_row metadata through ───────────────────────


def test_chunk_harvested_doc_carries_row_context() -> None:
    ctx = {
        "connection_id": "abc-123",
        "table": "tickets",
        "row_pk": {"id": 42},
        "extras": {"title": "Refund question"},
        "file_column": "attachment_url",
        "file_reference": "https://example.com/policy.pdf",
    }
    chunks = chunk_harvested_doc(
        "source-1", "policy.pdf",
        "Vacation policy is 24 days per year.",
        extra_metadata=ctx,
    )
    assert len(chunks) == 1
    md = chunks[0].metadata
    # Base harvested-doc fields are still there.
    assert md["source_id"] == "source-1"
    assert md["filename"] == "policy.pdf"
    assert md["chunk_index"] == 0
    # Row context is merged in.
    assert md["table"] == "tickets"
    assert md["row_pk"] == {"id": 42}
    assert md["extras"] == {"title": "Refund question"}
    assert md["file_column"] == "attachment_url"
    assert md["file_reference"] == "https://example.com/policy.pdf"


# ── Citations surface db_row ─────────────────────────────────────


def test_citations_attach_db_row_when_present() -> None:
    chunks = [
        {
            "kind": "harvested_doc",
            "source_key": "docsource:s1:policy.pdf:0",
            "text": "Document: policy.pdf\n\nRefund within 14 days.",
            "metadata": {
                "source_id": "s1",
                "filename": "policy.pdf",
                "chunk_index": 0,
                "connection_id": "abc",
                "table": "tickets",
                "row_pk": {"id": 42},
                "extras": {"title": "Refund question"},
                "file_column": "attachment_url",
                "file_reference": "https://example.com/policy.pdf",
            },
        }
    ]
    out = build_citations(chunks)
    assert len(out) == 1
    cit = out[0]
    assert cit["filename"] == "policy.pdf"
    assert "db_row" in cit
    row = cit["db_row"]
    assert row["table"] == "tickets"
    assert row["row_pk"] == {"id": 42}
    assert row["file_column"] == "attachment_url"
    assert row["extras"] == {"title": "Refund question"}


def test_citations_skip_db_row_when_no_table_link() -> None:
    chunks = [
        {
            "kind": "harvested_doc",
            "source_key": "docsource:s1:doc.pdf:0",
            "text": "Document: doc.pdf\n\nSome content.",
            "metadata": {
                "source_id": "s1",
                "filename": "doc.pdf",
                "chunk_index": 0,
                # NO table / row_pk / file_reference — a folder source.
            },
        }
    ]
    out = build_citations(chunks)
    assert "db_row" not in out[0]


def test_citation_hint_includes_row_pk_when_present() -> None:
    chunks = [
        {
            "kind": "harvested_doc",
            "source_key": "docsource:s1:a.pdf:0",
            "text": "Document: a.pdf\n\nHello.",
            "metadata": {
                "filename": "a.pdf",
                "source_id": "s1",
                "table": "tickets",
                "row_pk": {"id": 42},
                "file_reference": "https://x/a.pdf",
                "file_column": "url",
            },
        },
        {
            "kind": "harvested_doc",
            "source_key": "docsource:s1:b.pdf:0",
            "text": "Document: b.pdf\n\nWorld.",
            "metadata": {"filename": "b.pdf", "source_id": "s1"},
        },
    ]
    out = build_citations(chunks)
    hint = citation_hint_for_planner(out)
    # Row-linked source mentions the table+PK; folder source just the name.
    assert "[1] a.pdf (from tickets where id=42)" in hint
    assert "[2] b.pdf" in hint
    assert "(from " not in hint.split("[2]")[1]


def test_citation_hint_handles_composite_pk() -> None:
    chunks = [
        {
            "kind": "harvested_doc",
            "source_key": "docsource:s1:f.pdf:0",
            "text": "Document: f.pdf\n\nbody",
            "metadata": {
                "filename": "f.pdf",
                "source_id": "s1",
                "table": "user_files",
                "row_pk": {"user_id": 7, "file_id": 99},
                "file_reference": "https://x/f.pdf",
                "file_column": "url",
            },
        }
    ]
    out = build_citations(chunks)
    hint = citation_hint_for_planner(out)
    assert "user_files where" in hint
    assert "user_id=7" in hint
    assert "file_id=99" in hint


def test_citation_filename_handles_uzbek_russian_english() -> None:
    """Row-linkage shouldn't disturb multilingual passthrough."""
    chunks = [
        {
            "kind": "harvested_doc",
            "source_key": "docsource:s1:договор.pdf:0",
            "text": "Document: договор.pdf\n\nЦена возврата.",
            "metadata": {
                "filename": "договор.pdf",
                "source_id": "s1",
                "table": "tickets",
                "row_pk": {"id": 1},
                "file_reference": "https://x/договор.pdf",
                "file_column": "attachment",
            },
        }
    ]
    out = build_citations(chunks)
    cit = out[0]
    assert cit["filename"] == "договор.pdf"
    assert cit["db_row"]["table"] == "tickets"
    assert "Цена возврата" in cit["snippet"]
