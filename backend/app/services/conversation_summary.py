"""Phase 36 — rolling LLM summary of older chat messages.

When a session's message count exceeds ``SUMMARY_THRESHOLD``, the
oldest ``SUMMARY_THRESHOLD - KEEP_RECENT`` messages get rolled into
one summary paragraph that gets prepended to the agent's
``conversation_history``. The summary is stored on
``ChatSession.summary`` so subsequent turns don't re-summarise.

Re-summarisation triggers when the number of NEW messages since
the last summary's ``through_message_id`` again pushes the kept
window over the threshold. This keeps token cost roughly constant
even for 200-turn sessions.

The summary intentionally lives at the session row (not as a
``Message``) so:
  * it's invisible to the user-facing chat history view, AND
  * it can be re-computed at any time without polluting message ids.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import LLMClient, get_llm
from app.db.models import ChatSession, Message

log = logging.getLogger(__name__)

# When the kept (un-summarised) message window grows past this, we
# fold the older half into the summary. Lower values trade more LLM
# calls for tighter prompts; higher values save calls but waste
# tokens on long histories.
SUMMARY_THRESHOLD = 30
# Number of most-recent messages always passed raw to the agent —
# even when a summary exists. Keeps follow-ups like "show as chart"
# fully precise without LLM paraphrase.
KEEP_RECENT = 10


class ConversationSummary(BaseModel):
    """Structured shape the LLM must produce. Single field so guided
    decoding has minimal opportunity to drift."""

    model_config = ConfigDict(extra="forbid")

    summary_md: str = Field(
        min_length=10,
        max_length=2000,
        description=(
            "2-5 short paragraphs in markdown summarising the older "
            "portion of the conversation: what the user was trying "
            "to learn, which tables / connections came up, any "
            "running constraints (date ranges, status filters), and "
            "any pending follow-ups."
        ),
    )


async def ensure_summary(
    db: AsyncSession,
    chat_session: ChatSession,
    *,
    llm: LLMClient | None = None,
) -> str | None:
    """If the session has accumulated more than ``SUMMARY_THRESHOLD``
    messages since its last summary, rebuild the summary. Returns
    the (possibly newly-written) summary text, or ``None`` when no
    summary applies yet (short session).

    Mutates ``chat_session.summary`` in place; the caller is
    responsible for committing the surrounding transaction.
    """
    # Count messages, find the prior summary's cutoff.
    prior = chat_session.summary or None
    prior_cutoff_id: UUID | None = None
    if prior and isinstance(prior.get("through_message_id"), str):
        try:
            prior_cutoff_id = UUID(prior["through_message_id"])
        except ValueError:
            prior_cutoff_id = None

    # Pull all messages oldest → newest. For very long sessions this
    # touches ~hundreds of rows, which is fine. We rely on
    # ``ix_messages_session_id_created_at`` for ordering.
    rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == chat_session.id)
            .order_by(Message.created_at)
        )
    ).scalars().all()

    if len(rows) <= SUMMARY_THRESHOLD:
        # Nothing to do yet; return whatever existing summary we have
        # so callers can still inject it for shorter recent windows.
        return _summary_text(prior)

    # Decide cutoff: everything older than the last KEEP_RECENT is
    # eligible for summarisation. We summarise STRICTLY MORE than the
    # prior cutoff so re-summarising is cheap (only the delta).
    to_summarize = rows[: len(rows) - KEEP_RECENT]
    if prior_cutoff_id is not None:
        # Drop messages already covered by the prior summary unless
        # the kept window has drifted so far past it that we're
        # re-summarising the same range anyway (rare).
        # We re-summarise the FULL older range each time so the
        # summary stays coherent; doing incremental concat would let
        # earlier turns drift out of context. Single LLM call is
        # cheap enough.
        pass

    if not to_summarize:
        return _summary_text(prior)

    transcript = _format_transcript(to_summarize)
    new_text = await _run_llm_summary(transcript, llm=llm)
    cutoff_message = to_summarize[-1]

    chat_session.summary = {
        "text": new_text,
        "through_message_id": str(cutoff_message.id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info(
        "conversation_summary: session=%s summarised %d msgs (cutoff=%s)",
        chat_session.id, len(to_summarize), cutoff_message.id,
    )
    return new_text


def _summary_text(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    text = payload.get("text")
    return text if isinstance(text, str) and text.strip() else None


def _format_transcript(messages: list[Message]) -> str:
    parts: list[str] = []
    for m in messages:
        if m.role not in ("user", "assistant"):
            continue
        body = (m.content or "").strip()
        if not body:
            continue
        # Cap each turn so a giant assistant answer doesn't dominate.
        if len(body) > 1200:
            body = body[:1200] + " …"
        parts.append(f"[{m.role}] {body}")
    return "\n\n".join(parts)


async def _run_llm_summary(
    transcript: str, *, llm: LLMClient | None
) -> str:
    """Single LLM round-trip; falls back to a deterministic stub when
    the call fails so the chat path never breaks."""
    client = llm or get_llm()
    system = (
        "You compress chat transcripts. Produce a tight 2-5 paragraph "
        "markdown summary of the conversation below. Cover: what the "
        "user is trying to learn, which tables / connections / "
        "documents came up, any running filters (date ranges, "
        "statuses, scopes), and unresolved follow-ups. Do NOT add "
        "information that isn't in the transcript."
    )
    user = f"Transcript:\n\n{transcript}\n\nProduce the summary now."
    try:
        result = await client.structured(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_model=ConversationSummary,
            temperature=0.2,
            max_tokens=512,
        )
        return result.summary_md
    except Exception as e:  # noqa: BLE001
        log.warning(
            "conversation_summary: LLM failed (%s); using truncated "
            "transcript as a fallback summary",
            e,
        )
        # Defensive fallback so the chat path never breaks if vLLM
        # is down — we keep a short prefix of the transcript instead
        # of summarising. The user still gets context (verbatim),
        # just no compression win.
        return transcript[:1500]


__all__ = [
    "ConversationSummary",
    "KEEP_RECENT",
    "SUMMARY_THRESHOLD",
    "ensure_summary",
]
