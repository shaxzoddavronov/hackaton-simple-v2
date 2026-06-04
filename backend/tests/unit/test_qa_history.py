"""Phase 38 — qa_history indexing + similarity-search unit tests.

The pgvector + asyncpg path is exercised by the integration suite
against a real Postgres. Here we lock down the pure-Python pieces
(threshold gate, min-length skip, vector formatter, Triton-failure
fallbacks) without spinning up a DB.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.qa_history import (
    DEFAULT_TOP_K,
    MIN_QUESTION_LEN,
    QaHit,
    SIMILARITY_THRESHOLD,
    _format_vector,
    find_similar,
    index_qa_pair,
)


# ── helpers ──────────────────────────────────────────────────────


class _FakeSession:
    """Records every execute() call so the test can assert the
    intended INSERT / SELECT shapes without a live DB."""

    def __init__(self, mapping_rows: list[dict] | None = None) -> None:
        self.executes: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0
        self._mapping_rows = mapping_rows or []

    async def execute(self, stmt, params=None):
        self.executes.append((stmt, params))

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def mappings(self):
                return iter(self._rows)

        return _Result(self._mapping_rows)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _vec(dim: int = 1024, fill: float = 0.1) -> list[float]:
    return [fill] * dim


# ── _format_vector ───────────────────────────────────────────────


def test_format_vector_brackets_and_commas() -> None:
    out = _format_vector([0.1, 0.2, 0.3])
    assert out.startswith("[") and out.endswith("]")
    assert "0.1000000" in out
    assert out.count(",") == 2


def test_format_vector_empty() -> None:
    assert _format_vector([]) == "[]"


# ── threshold constants sane ────────────────────────────────────


def test_threshold_is_strict_enough() -> None:
    # 0.85 keeps multilingual paraphrase pairs (cos~0.74-0.78 from
    # the earlier smoke test) out of the chip rail. Don't lower
    # without thinking about false-positive cost.
    assert SIMILARITY_THRESHOLD >= 0.8


def test_default_top_k_is_small() -> None:
    # The chip rail can only display a few; large K wastes Triton
    # round-trip + DB ranking.
    assert 1 <= DEFAULT_TOP_K <= 5


def test_min_question_len_skips_trivial_inputs() -> None:
    assert MIN_QUESTION_LEN >= 4


# ── index_qa_pair ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_qa_pair_short_question_is_skipped() -> None:
    db = _FakeSession()
    await index_qa_pair(
        db,  # type: ignore[arg-type]
        workspace_id=uuid4(),
        message_id=uuid4(),
        session_id=uuid4(),
        question="hi",  # below MIN_QUESTION_LEN
        headline="anything",
    )
    # No DB call, no Triton call.
    assert db.executes == []


@pytest.mark.asyncio
async def test_index_qa_pair_triton_failure_is_swallowed() -> None:
    db = _FakeSession()

    fake_client = SimpleNamespace(
        embed=AsyncMock(side_effect=RuntimeError("triton down"))
    )
    with patch(
        "app.services.qa_history.get_client", return_value=fake_client
    ):
        await index_qa_pair(
            db,  # type: ignore[arg-type]
            workspace_id=uuid4(),
            message_id=uuid4(),
            session_id=uuid4(),
            question="What is the total revenue this quarter?",
            headline="Revenue: $1.2M",
        )
    # Triton failed → no insert attempted, no commit.
    assert db.executes == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_index_qa_pair_happy_path_inserts_and_commits() -> None:
    db = _FakeSession()
    fake_client = SimpleNamespace(
        embed=AsyncMock(
            return_value=SimpleNamespace(vectors=[_vec()])
        )
    )
    ws_id, msg_id, sess_id = uuid4(), uuid4(), uuid4()
    with patch(
        "app.services.qa_history.get_client", return_value=fake_client
    ):
        await index_qa_pair(
            db,  # type: ignore[arg-type]
            workspace_id=ws_id,
            message_id=msg_id,
            session_id=sess_id,
            question="How many users registered last week?",
            headline="42 users",
        )
    assert len(db.executes) == 1
    assert db.commits == 1
    _, params = db.executes[0]
    assert params["workspace_id"] == ws_id
    assert params["source_key"] == f"qa::{msg_id}"
    # Metadata is JSON-encoded for the CAST(:metadata AS jsonb) bind.
    import json

    md = json.loads(params["metadata"])
    assert md["question"].startswith("How many users")
    assert md["headline"] == "42 users"
    assert md["message_id"] == str(msg_id)
    assert md["session_id"] == str(sess_id)


@pytest.mark.asyncio
async def test_index_qa_pair_insert_failure_rolls_back() -> None:
    """If the INSERT raises (constraint violation, bad input, etc.)
    we must rollback so the surrounding chat-turn transaction stays
    clean."""
    db = _FakeSession()

    async def boom(*a, **kw):
        raise RuntimeError("constraint violated")

    db.execute = boom  # type: ignore[assignment]

    fake_client = SimpleNamespace(
        embed=AsyncMock(return_value=SimpleNamespace(vectors=[_vec()]))
    )
    with patch(
        "app.services.qa_history.get_client", return_value=fake_client
    ):
        await index_qa_pair(
            db,  # type: ignore[arg-type]
            workspace_id=uuid4(),
            message_id=uuid4(),
            session_id=uuid4(),
            question="A long enough question to pass the gate",
            headline="answer",
        )
    assert db.rollbacks == 1


# ── find_similar ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_similar_short_question_returns_empty() -> None:
    hits = await find_similar(
        _FakeSession(),  # type: ignore[arg-type]
        workspace_id=uuid4(),
        question="x",
    )
    assert hits == []


@pytest.mark.asyncio
async def test_find_similar_triton_failure_returns_empty() -> None:
    fake_client = SimpleNamespace(
        embed=AsyncMock(side_effect=RuntimeError("triton down"))
    )
    with patch(
        "app.services.qa_history.get_client", return_value=fake_client
    ):
        hits = await find_similar(
            _FakeSession(),  # type: ignore[arg-type]
            workspace_id=uuid4(),
            question="How many users registered?",
        )
    assert hits == []


@pytest.mark.asyncio
async def test_find_similar_filters_below_threshold() -> None:
    """Only hits at or above the threshold should surface."""
    rows = [
        {
            "chunk_metadata": {
                "question": "old similar question",
                "headline": "42",
                "message_id": "m-1",
                "session_id": "s-1",
            },
            "similarity": 0.9,
        },
        {
            "chunk_metadata": {
                "question": "an unrelated question",
                "headline": "n/a",
                "message_id": "m-2",
                "session_id": "s-2",
            },
            "similarity": 0.4,
        },
    ]
    db = _FakeSession(mapping_rows=rows)
    fake_client = SimpleNamespace(
        embed=AsyncMock(return_value=SimpleNamespace(vectors=[_vec()]))
    )
    with patch(
        "app.services.qa_history.get_client", return_value=fake_client
    ):
        hits = await find_similar(
            db,  # type: ignore[arg-type]
            workspace_id=uuid4(),
            question="How many users registered last week?",
        )
    assert len(hits) == 1
    assert hits[0].similarity == 0.9
    assert hits[0].question == "old similar question"


@pytest.mark.asyncio
async def test_find_similar_decodes_json_string_metadata() -> None:
    """Some drivers return JSONB columns as raw JSON strings; we
    must decode rather than treat as text."""
    import json

    rows = [
        {
            "chunk_metadata": json.dumps(
                {
                    "question": "string-encoded metadata",
                    "headline": "x",
                    "message_id": "m-1",
                    "session_id": "s-1",
                }
            ),
            "similarity": 0.95,
        },
    ]
    fake_client = SimpleNamespace(
        embed=AsyncMock(return_value=SimpleNamespace(vectors=[_vec()]))
    )
    with patch(
        "app.services.qa_history.get_client", return_value=fake_client
    ):
        hits = await find_similar(
            _FakeSession(mapping_rows=rows),  # type: ignore[arg-type]
            workspace_id=uuid4(),
            question="Long enough question to embed",
        )
    assert len(hits) == 1
    assert hits[0].question == "string-encoded metadata"


@pytest.mark.asyncio
async def test_find_similar_returns_typed_qahit() -> None:
    rows = [
        {
            "chunk_metadata": {
                "question": "previous Q",
                "headline": "previous A",
                "message_id": "mmm",
                "session_id": "sss",
            },
            "similarity": 0.91,
        },
    ]
    fake_client = SimpleNamespace(
        embed=AsyncMock(return_value=SimpleNamespace(vectors=[_vec()]))
    )
    with patch(
        "app.services.qa_history.get_client", return_value=fake_client
    ):
        hits = await find_similar(
            _FakeSession(mapping_rows=rows),  # type: ignore[arg-type]
            workspace_id=uuid4(),
            question="Long enough question to embed",
        )
    assert isinstance(hits[0], QaHit)
    assert hits[0].similarity == 0.91
