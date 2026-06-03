"""Phase 36 — rolling LLM summary tests.

Threshold logic and DB mutations are deterministic — only the LLM
call itself needs mocking. We patch :func:`get_llm` so no real
network round-trip happens.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.conversation_summary import (
    KEEP_RECENT,
    SUMMARY_THRESHOLD,
    ConversationSummary,
    _format_transcript,
    _summary_text,
    ensure_summary,
)


# ── helpers ──────────────────────────────────────────────────


def _msg(role: str, content: str, *, t: datetime, mid=None):
    return SimpleNamespace(
        id=mid or uuid4(),
        role=role,
        content=content,
        created_at=t,
    )


def _fake_session(messages: list, summary=None):
    """A minimal SQLAlchemy-like AsyncSession that returns `messages`
    in oldest→newest order for the one query the service makes."""

    class _ScalarResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return list(self._items)

    class _AsyncSession:
        def __init__(self, items):
            self._items = items
            self.executed = []

        async def execute(self, stmt):
            self.executed.append(stmt)
            return _ScalarResult(self._items)

    chat_session = SimpleNamespace(id=uuid4(), summary=summary)
    db = _AsyncSession(messages)
    return db, chat_session


# ── _summary_text ────────────────────────────────────────────


def test_summary_text_handles_none() -> None:
    assert _summary_text(None) is None


def test_summary_text_handles_empty_text() -> None:
    assert _summary_text({"text": "  "}) is None


def test_summary_text_returns_text() -> None:
    assert _summary_text({"text": "alright"}) == "alright"


# ── _format_transcript ───────────────────────────────────────


def test_format_transcript_skips_system_and_empty() -> None:
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    msgs = [
        _msg("system", "skip me", t=base),
        _msg("user", "", t=base + timedelta(seconds=1)),
        _msg("user", "real question", t=base + timedelta(seconds=2)),
        _msg("assistant", "real answer", t=base + timedelta(seconds=3)),
    ]
    out = _format_transcript(msgs)
    assert "[user] real question" in out
    assert "[assistant] real answer" in out
    assert "system" not in out


def test_format_transcript_caps_long_turns() -> None:
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    msgs = [_msg("assistant", "x" * 5000, t=base)]
    out = _format_transcript(msgs)
    assert len(out) < 2000
    assert out.endswith("…")


# ── ensure_summary — threshold gate ──────────────────────────


@pytest.mark.asyncio
async def test_ensure_summary_returns_none_for_short_session() -> None:
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    msgs = [
        _msg("user", f"q{i}", t=base + timedelta(seconds=i))
        for i in range(SUMMARY_THRESHOLD - 5)
    ]
    db, chat = _fake_session(msgs)
    result = await ensure_summary(db, chat)
    assert result is None
    assert chat.summary is None


@pytest.mark.asyncio
async def test_ensure_summary_returns_prior_when_under_threshold() -> None:
    """Short session WITH an old summary should keep returning the
    old summary text untouched."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    msgs = [_msg("user", "q", t=base)]
    prior = {
        "text": "an earlier summary",
        "through_message_id": str(uuid4()),
        "updated_at": "2026-06-01T00:00:00+00:00",
    }
    db, chat = _fake_session(msgs, summary=prior)
    result = await ensure_summary(db, chat)
    assert result == "an earlier summary"


# ── ensure_summary — happy path ──────────────────────────────


@pytest.mark.asyncio
async def test_ensure_summary_writes_new_summary_when_over_threshold() -> None:
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    msgs = [
        _msg(
            "user" if i % 2 == 0 else "assistant",
            f"turn-{i} content",
            t=base + timedelta(seconds=i),
        )
        for i in range(SUMMARY_THRESHOLD + 10)
    ]
    db, chat = _fake_session(msgs)

    fake_llm = SimpleNamespace(
        structured=AsyncMock(
            return_value=ConversationSummary(
                summary_md="User asked about user counts and quizzes."
            )
        )
    )
    with patch(
        "app.services.conversation_summary.get_llm",
        return_value=fake_llm,
    ):
        result = await ensure_summary(db, chat)

    assert result == "User asked about user counts and quizzes."
    assert chat.summary is not None
    assert chat.summary["text"] == result
    # Cutoff is the LAST message that fell into the summarised
    # window — i.e. the message at index (total - KEEP_RECENT - 1).
    cutoff_idx = len(msgs) - KEEP_RECENT - 1
    assert chat.summary["through_message_id"] == str(msgs[cutoff_idx].id)
    fake_llm.structured.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_summary_falls_back_to_truncation_on_llm_failure() -> None:
    """If vLLM is down the chat path MUST NOT break — we surface a
    deterministic short prefix as the summary."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    msgs = [
        _msg("user", f"long message {i} " * 5, t=base + timedelta(seconds=i))
        for i in range(SUMMARY_THRESHOLD + 5)
    ]
    db, chat = _fake_session(msgs)

    fake_llm = SimpleNamespace(
        structured=AsyncMock(side_effect=RuntimeError("vLLM down"))
    )
    with patch(
        "app.services.conversation_summary.get_llm",
        return_value=fake_llm,
    ):
        result = await ensure_summary(db, chat)

    assert result is not None
    # Fallback uses the first 1500 chars of the formatted transcript.
    assert "long message" in result
    # Summary still got persisted so we don't retry LLM on every turn.
    assert chat.summary is not None
    assert chat.summary["text"] == result


@pytest.mark.asyncio
async def test_ensure_summary_supplies_llm_arg_overrides_factory() -> None:
    """Caller can inject a specific LLM client — useful for tests
    and for future per-workspace model selection."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    msgs = [
        _msg("user", "q", t=base + timedelta(seconds=i))
        for i in range(SUMMARY_THRESHOLD + 5)
    ]
    db, chat = _fake_session(msgs)

    injected = SimpleNamespace(
        structured=AsyncMock(
            return_value=ConversationSummary(
                summary_md="custom client summary"
            )
        )
    )
    # No need to patch get_llm — we pass llm= directly.
    result = await ensure_summary(db, chat, llm=injected)
    assert result == "custom client summary"
    injected.structured.assert_awaited_once()


# ── ConversationSummary contract ─────────────────────────────


def test_summary_model_rejects_empty_text() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConversationSummary(summary_md="")


def test_summary_model_rejects_overly_long_text() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConversationSummary(summary_md="x" * 5000)


def test_summary_threshold_constants_sane() -> None:
    """Guardrails: KEEP_RECENT must always leave room for older
    history to fold into the summary."""
    assert KEEP_RECENT < SUMMARY_THRESHOLD
    assert KEEP_RECENT >= 4  # enough room for one full ask-and-followup
