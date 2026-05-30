"""Phase 25 — hybrid retrieval (dense + BM25 with RRF fusion).

The dense path is mocked because tests don't run Triton. BM25 over
SQLite is exercised against a real in-memory schema seeded with a
handful of chunks, so we verify both the BM25 ranking and the RRF
combination.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.services.rag.retriever import (
    RRF_K,
    RetrievedChunk,
    _fuse_rrf,
    _tokenise,
)


# ── _tokenise ────────────────────────────────────────────────────


def test_tokenise_basic() -> None:
    assert _tokenise("Hello, world!") == ["hello", "world"]


def test_tokenise_lower_case() -> None:
    assert _tokenise("Refund POLICY") == ["refund", "policy"]


def test_tokenise_keeps_cyrillic_and_uzbek() -> None:
    # bge-m3 handles cross-lingual semantics, but BM25's job is
    # exact-term matching — so the tokeniser must preserve
    # non-ASCII letters.
    assert _tokenise("Salom Алиса!") == ["salom", "алиса"]


def test_tokenise_drops_punctuation_and_underscores() -> None:
    assert _tokenise("error_code-42") == ["error", "code", "42"]


def test_tokenise_empty_input() -> None:
    assert _tokenise("") == []
    assert _tokenise(None) == []  # type: ignore[arg-type]


# ── _fuse_rrf ────────────────────────────────────────────────────


def _chunk(id_: str, score: float = 0.0, text: str = "") -> RetrievedChunk:
    return RetrievedChunk(
        id=id_,
        kind="schema_table",
        source_key=f"key:{id_}",
        text=text,
        score=score,
        metadata={},
    )


def test_fuse_rrf_intersection_rises() -> None:
    """A chunk appearing in BOTH lists must rank above one that
    appears in only one."""
    dense = [_chunk("a"), _chunk("b"), _chunk("c")]
    bm25 = [_chunk("c"), _chunk("d"), _chunk("a")]
    fused = _fuse_rrf(dense, bm25, top_k=4)
    # a (rank 0 in dense, rank 2 in bm25) AND c (rank 2 in dense,
    # rank 0 in bm25) both appear in both lists. They should be top
    # two. b and d each appear in only one list and rank below.
    ids = [c.id for c in fused]
    assert set(ids[:2]) == {"a", "c"}
    assert ids[2] in {"b", "d"}
    assert ids[3] in {"b", "d"}


def test_fuse_rrf_handles_empty_dense() -> None:
    """Triton-down scenario: dense list empty, BM25 alone drives
    ordering."""
    bm25 = [_chunk("a"), _chunk("b"), _chunk("c")]
    fused = _fuse_rrf([], bm25, top_k=2)
    assert [c.id for c in fused] == ["a", "b"]


def test_fuse_rrf_handles_empty_bm25() -> None:
    dense = [_chunk("x"), _chunk("y")]
    fused = _fuse_rrf(dense, [], top_k=2)
    assert [c.id for c in fused] == ["x", "y"]


def test_fuse_rrf_top_k_truncation() -> None:
    dense = [_chunk(str(i)) for i in range(10)]
    bm25 = [_chunk(str(i + 100)) for i in range(10)]
    fused = _fuse_rrf(dense, bm25, top_k=3)
    assert len(fused) == 3


def test_fuse_rrf_overwrites_score_with_fused_value() -> None:
    """The returned chunks' .score reflects the FUSED score, not
    the original per-retriever score — so downstream consumers
    sort by a single signal."""
    dense = [_chunk("a", score=0.99)]
    bm25 = [_chunk("a", score=0.42)]
    fused = _fuse_rrf(dense, bm25, top_k=1)
    # 1/(60+1) + 1/(60+1) = 2/61 ≈ 0.0328
    assert abs(fused[0].score - (2 / (RRF_K + 1))) < 1e-6


def test_fuse_rrf_chunk_appearing_only_in_dense_keeps_its_payload() -> None:
    """When a chunk lives in only one list, its text/metadata still
    survive the fusion (no None / empty)."""
    dense = [_chunk("a", text="from dense")]
    fused = _fuse_rrf(dense, [], top_k=1)
    assert fused[0].text == "from dense"
    assert fused[0].source_key == "key:a"


# ── End-to-end SQLite BM25 + dense fusion ───────────────────────


@pytest.mark.asyncio
async def test_retrieve_bm25_only_when_triton_disabled(
    monkeypatch,
) -> None:
    """When Triton is disabled, dense path returns [] and BM25
    drives the result entirely. Uses a hand-rolled SQLite schema
    (the ORM has Postgres-specific ``server_default`` clauses
    SQLite can't parse) seeded with a few chunks.
    """
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker, create_async_engine,
    )

    from app.services.rag.retriever import retrieve

    fake_client = MagicMock()
    fake_client.enabled = False
    with patch(
        "app.services.rag.retriever.get_client",
        return_value=fake_client,
    ):
        eng = create_async_engine(
            "sqlite+aiosqlite:///:memory:", future=True
        )
        ws_id = UUID("11111111-1111-1111-1111-111111111111")
        async with eng.begin() as conn:
            await conn.execute(
                sa_text(
                    "CREATE TABLE rag_chunks ("
                    " id TEXT PRIMARY KEY, "
                    " workspace_id TEXT, "
                    " connection_id TEXT, "
                    " document_id TEXT, "
                    " kind TEXT NOT NULL, "
                    " source_key TEXT NOT NULL, "
                    " text TEXT NOT NULL, "
                    " embedding TEXT, "
                    " chunk_metadata TEXT NOT NULL DEFAULT '{}', "
                    " content_hash TEXT NOT NULL, "
                    " updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
                )
            )
            for i, (sk, body) in enumerate(
                [
                    ("t.users", "The users table holds account info"),
                    (
                        "t.refunds",
                        "Refund policy: 14 days for ESF-4421 errors",
                    ),
                    ("t.orders", "orders include order_id and amount"),
                ]
            ):
                await conn.execute(
                    sa_text(
                        "INSERT INTO rag_chunks ("
                        " id, workspace_id, kind, source_key, text, "
                        " content_hash, chunk_metadata"
                        ") VALUES (:id, :ws, :kind, :sk, :body, :h, '{}')"
                    ),
                    {
                        "id": f"row-{i}",
                        "ws": str(ws_id),
                        "kind": "schema_table",
                        "sk": sk,
                        "body": body,
                        "h": f"h{i}",
                    },
                )

        Session = async_sessionmaker(eng, expire_on_commit=False)
        async with Session() as session:
            # BM25 should find the refund chunk for an exact-term
            # query like the error code, even with no dense signal.
            out = await retrieve(
                session,
                query="ESF-4421 error",
                workspace_id=ws_id,
                top_k=3,
            )
            ids = [c.source_key for c in out]
            assert "t.refunds" in ids
            assert out[0].source_key == "t.refunds"
        await eng.dispose()


@pytest.mark.asyncio
async def test_retrieve_empty_query_returns_empty() -> None:
    from app.services.rag.retriever import retrieve

    # Session not used; we never reach DB on an empty query.
    out = await retrieve(
        MagicMock(),
        query="",
        workspace_id=None,
    )
    assert out == []


@pytest.mark.asyncio
async def test_retrieve_whitespace_only_query_returns_empty() -> None:
    from app.services.rag.retriever import retrieve

    out = await retrieve(MagicMock(), query="   \n\t  ", workspace_id=None)
    assert out == []
