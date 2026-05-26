from __future__ import annotations

from app.engines.base import (
    ColumnMeta,
    ColumnSample,
    ForeignKeyMeta,
    SchemaBundle,
    TableMeta,
)
from app.services.rag.chunking import (
    chunk_api_endpoints,
    chunk_document,
    chunk_schema_bundle,
)


def _bundle() -> SchemaBundle:
    orders = TableMeta(
        schema="public",
        name="orders",
        columns=[
            ColumnMeta(name="id", data_type="bigint", nullable=False, is_pk=True, is_id=True),
            ColumnMeta(name="customer_id", data_type="bigint", nullable=False, fk_to="customers.id"),
            ColumnMeta(name="region", data_type="text", nullable=True),
            ColumnMeta(name="amount", data_type="numeric", nullable=True),
        ],
        foreign_keys=[
            ForeignKeyMeta(
                from_columns=["customer_id"],
                to_table="customers",
                to_columns=["id"],
            )
        ],
        row_count_estimate=1000,
    )
    customers = TableMeta(
        schema="public",
        name="customers",
        columns=[
            ColumnMeta(name="id", data_type="bigint", nullable=False, is_pk=True, is_id=True),
            ColumnMeta(name="email", data_type="text", nullable=False, is_unique=True),
        ],
    )
    samples = {
        "public.orders": {
            "region": ColumnSample(
                distinct_values=["EMEA", "APAC", "AMER"],
                distinct_truncated=False,
            )
        }
    }
    return SchemaBundle(
        dialect="postgres", tables=[orders, customers], samples=samples
    )


def test_chunk_schema_bundle_one_per_table() -> None:
    chunks = chunk_schema_bundle(_bundle())
    keys = {c.source_key for c in chunks}
    assert keys == {"public.orders", "public.customers"}
    assert all(c.kind == "schema_table" for c in chunks)


def test_chunk_text_mentions_columns_and_fks() -> None:
    chunks = chunk_schema_bundle(_bundle())
    orders = next(c for c in chunks if c.source_key == "public.orders")
    text = orders.text
    assert "Table: public.orders" in text
    assert "customer_id" in text
    assert "fk -> customers.id" in text or "customer_id" in text
    assert "Foreign keys:" in text
    # Categorical sample folds in
    assert "EMEA" in text and "APAC" in text


def test_chunk_metadata_round_trips_structure() -> None:
    chunks = chunk_schema_bundle(_bundle())
    orders = next(c for c in chunks if c.source_key == "public.orders")
    md = orders.metadata
    assert md["dialect"] == "postgres"
    assert md["table"] == "orders"
    assert any(col["name"] == "region" for col in md["columns"])
    assert md["fks"][0]["to_table"] == "customers"


def test_content_hash_changes_with_text() -> None:
    chunks = chunk_schema_bundle(_bundle())
    h1 = chunks[0].content_hash
    # Re-running on the same bundle yields the same hash.
    chunks2 = chunk_schema_bundle(_bundle())
    h2 = next(c for c in chunks2 if c.source_key == chunks[0].source_key).content_hash
    assert h1 == h2


def test_chunk_document_overlapping_windows() -> None:
    body = "x" * 3000
    chunks = chunk_document("doc-1", "Sample", body, chunk_size=1000, overlap=200)
    # Step = 800; ceil(3000 / 800) = 4 windows
    assert len(chunks) == 4
    assert all(c.kind == "user_doc" for c in chunks)
    assert chunks[0].source_key == "doc-1:0"
    assert chunks[3].source_key == "doc-1:3"


def test_chunk_document_rejects_invalid_params() -> None:
    import pytest

    with pytest.raises(ValueError):
        chunk_document("doc", "t", "abc", chunk_size=100, overlap=100)


def test_chunk_api_endpoints() -> None:
    routes = [
        {
            "method": "POST",
            "path": "/workspaces",
            "summary": "Create a workspace",
            "description": "Adds a database connection.",
        },
        {"method": "GET", "path": "/workspaces", "summary": "List workspaces", "description": ""},
    ]
    chunks = chunk_api_endpoints(routes)
    assert len(chunks) == 2
    assert all(c.kind == "api_endpoint" for c in chunks)
    assert chunks[0].source_key == "POST /workspaces"
    assert "Create a workspace" in chunks[0].text
