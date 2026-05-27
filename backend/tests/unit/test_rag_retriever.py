"""Retriever end-to-end against in-memory SQLite, with a fake Triton client.

Verifies:
  - Workspace-scoped retrieval orders by cosine similarity.
  - Global (workspace_id=NULL) chunks are mixed in when ``include_global=True``.
  - Triton failure → empty result (caller falls back to BM25).
"""
from __future__ import annotations

import json
from typing import Sequence
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.rag import retriever as retriever_mod
from app.services.rag import triton_client as triton_mod
from app.services.rag.retriever import retrieve
from app.services.rag.triton_client import EmbeddingResponse, TritonUnavailable


class _FakeTriton:
    """In-process embedding stub keyed on substring hits.

    Each known keyword maps to a distinct 4-dim basis vector so cosine
    similarity reduces to "did the chunk text contain the keyword".
    """

    def __init__(self) -> None:
        self.enabled = True
        self.dim = 4
        self.model = "fake"

    async def embed(self, texts: Sequence[str]) -> EmbeddingResponse:
        vectors = [_embed(t) for t in texts]
        return EmbeddingResponse(vectors=vectors, model=self.model, dim=self.dim)


def _embed(text: str) -> list[float]:
    text = text.lower()
    v = [0.0, 0.0, 0.0, 0.0]
    if "orders" in text or "revenue" in text or "sales" in text:
        v[0] += 1.0
    if "customers" in text or "users" in text:
        v[1] += 1.0
    if "products" in text or "inventory" in text:
        v[2] += 1.0
    if "api" in text or "endpoint" in text or "workspace" in text:
        v[3] += 1.0
    s = sum(x * x for x in v) ** 0.5
    if s == 0:
        return [1.0, 0.0, 0.0, 0.0]
    return [x / s for x in v]


class _FailingTriton:
    enabled = True
    dim = 4
    model = "fake-fail"

    async def embed(self, texts):  # type: ignore[no-untyped-def]
        raise TritonUnavailable("boom")


@pytest_asyncio.fixture
async def session():
    """In-memory SQLite with only the tables this test needs.

    We can't use ``Base.metadata.create_all`` because some pre-existing
    models declare Postgres-only server defaults (e.g. ``'{}'::jsonb``)
    that error on SQLite. Issuing raw DDL keeps the test self-contained.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT, password_hash TEXT)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE workspaces (id TEXT PRIMARY KEY, owner_id TEXT, "
            "name TEXT, dialect TEXT, status TEXT)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE uploaded_documents (id TEXT PRIMARY KEY, owner_id TEXT, "
            "workspace_id TEXT, title TEXT, mime_type TEXT, body TEXT, "
            "created_at TIMESTAMP)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE rag_chunks ("
            "id TEXT PRIMARY KEY, workspace_id TEXT, connection_id TEXT, "
            "document_id TEXT, kind TEXT NOT NULL, source_key TEXT NOT NULL, "
            "text TEXT NOT NULL, embedding JSON, "
            "chunk_metadata JSON NOT NULL DEFAULT '{}', "
            "content_hash TEXT NOT NULL, updated_at TIMESTAMP)"
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(session):
    """Seed via raw SQL — bypasses ORM-level UUID binding on SQLite, which
    is a known quirk when tables are created out-of-band with raw DDL."""
    from sqlalchemy import text as sa_text

    workspace_id = uuid4()
    rows = [
        (workspace_id, "schema_table", "public.orders", "orders revenue table"),
        (workspace_id, "schema_table", "public.customers", "customers users table"),
        (workspace_id, "schema_table", "public.products", "products inventory table"),
        (None, "api_endpoint", "POST /workspaces", "API endpoint: add workspace"),
    ]
    for wid, kind, key, text in rows:
        await session.execute(
            sa_text(
                "INSERT INTO rag_chunks (id, workspace_id, kind, source_key, text, "
                "embedding, chunk_metadata, content_hash) "
                "VALUES (:id, :wid, :kind, :key, :text, :emb, :md, :h)"
            ),
            {
                "id": str(uuid4()),
                "wid": str(wid) if wid else None,
                "kind": kind,
                "key": key,
                "text": text,
                "emb": json.dumps(_embed(text)),
                "md": json.dumps({"schema": "public", "table": key.split(".")[-1]}),
                "h": f"h-{key}",
            },
        )
    await session.commit()
    return {"workspace_id": workspace_id}


def _install_fake(monkeypatch, client):
    triton_mod.reset_client_for_tests()
    # ``retriever`` imports ``get_client`` by name, so patch the binding
    # in the retriever module too — patching only ``triton_mod`` would not
    # affect the already-resolved reference inside retriever.
    monkeypatch.setattr(triton_mod, "get_client", lambda: client)
    monkeypatch.setattr(retriever_mod, "get_client", lambda: client)


@pytest.mark.asyncio
async def test_retrieve_orders_question_pulls_orders_chunk(session, seeded, monkeypatch):
    _install_fake(monkeypatch, _FakeTriton())
    hits = await retrieve(
        session,
        query="total revenue by region",
        workspace_id=seeded["workspace_id"],
        top_k=3,
    )
    assert hits, "expected at least one hit"
    assert hits[0].source_key == "public.orders"


@pytest.mark.asyncio
async def test_retrieve_global_chunks_with_include_global(session, seeded, monkeypatch):
    _install_fake(monkeypatch, _FakeTriton())
    hits = await retrieve(
        session,
        query="how do I add a workspace via api?",
        workspace_id=seeded["workspace_id"],
        top_k=5,
        include_global=True,
    )
    keys = [h.source_key for h in hits]
    assert "POST /workspaces" in keys


@pytest.mark.asyncio
async def test_retrieve_returns_empty_on_triton_failure(session, seeded, monkeypatch):
    _install_fake(monkeypatch, _FailingTriton())
    hits = await retrieve(
        session,
        query="orders",
        workspace_id=seeded["workspace_id"],
        top_k=3,
    )
    assert hits == []


@pytest.mark.asyncio
async def test_retrieve_empty_query_returns_empty(session, seeded, monkeypatch):
    _install_fake(monkeypatch, _FakeTriton())
    hits = await retrieve(
        session, query="   ", workspace_id=seeded["workspace_id"], top_k=3
    )
    assert hits == []
