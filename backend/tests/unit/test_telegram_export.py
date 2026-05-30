"""Phase 22 — Telegram Desktop chat-export JSON harvester."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from app.services.doc_harvest import (
    _format_telegram_day,
    _telegram_text,
    harvest_telegram_export,
)


# ── _telegram_text — flatten plain | run-list ────────────────────


def test_telegram_text_plain_string() -> None:
    assert _telegram_text("hello world") == "hello world"


def test_telegram_text_run_list_flattened() -> None:
    runs = [
        {"type": "plain", "text": "Click "},
        {"type": "link", "text": "here", "href": "https://x"},
        {"type": "plain", "text": " now"},
    ]
    assert _telegram_text(runs) == "Click here now"


def test_telegram_text_mixed_string_and_dict() -> None:
    runs = ["raw ", {"type": "bold", "text": "BOLD"}, " end"]
    assert _telegram_text(runs) == "raw BOLD end"


def test_telegram_text_unknown_shape_returns_empty() -> None:
    assert _telegram_text(42) == ""
    assert _telegram_text(None) == ""


# ── _format_telegram_day ────────────────────────────────────────


def test_format_day_emits_sender_time_body() -> None:
    msgs = [
        {
            "from": "Alice",
            "date": "2026-05-29T09:15:00",
            "text": "morning standup?",
        },
        {
            "from": "Bob",
            "date": "2026-05-29T09:16:30",
            "text": "joining now",
        },
    ]
    text, first, last = _format_telegram_day(msgs)
    assert "Alice (09:15)" in text
    assert "morning standup?" in text
    assert "Bob (09:16)" in text
    assert first == "Alice"
    assert last == "Bob"


def test_format_day_includes_reply_marker() -> None:
    msgs = [
        {
            "from": "Alice",
            "date": "2026-05-29T09:00:00",
            "text": "?",
            "reply_to_message_id": 12345,
        }
    ]
    text, _, _ = _format_telegram_day(msgs)
    assert "↳ 12345" in text


def test_format_day_media_only_message_kept() -> None:
    msgs = [
        {
            "from": "Carol",
            "date": "2026-05-29T10:00:00",
            "text": "",
            "media_type": "voice_message",
            "duration_seconds": 17,
        }
    ]
    text, _, _ = _format_telegram_day(msgs)
    assert "[voice_message" in text
    assert "17s" in text


def test_format_day_empty_messages_yields_empty() -> None:
    assert _format_telegram_day([]) == ("", "", "")


# ── harvester end-to-end ─────────────────────────────────────────


def _write_export(
    tmp_path: Path,
    *,
    messages: list[dict],
    name: str = "Team chat",
    chat_id: int = 99887766,
    chat_type: str = "private_group",
) -> Path:
    payload = {
        "name": name,
        "id": chat_id,
        "type": chat_type,
        "messages": messages,
    }
    p = tmp_path / "telegram_export.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_harvest_telegram_groups_by_day(tmp_path: Path) -> None:
    path = _write_export(
        tmp_path,
        messages=[
            {
                "id": 1,
                "type": "message",
                "from": "Alice",
                "date": "2026-05-29T09:00:00",
                "text": "Refund queue is at 12 today",
            },
            {
                "id": 2,
                "type": "message",
                "from": "Bob",
                "date": "2026-05-29T09:05:00",
                "text": "I'll take 5",
            },
            {
                "id": 3,
                "type": "message",
                "from": "Alice",
                "date": "2026-05-30T08:00:00",
                "text": "Down to 4 — thanks Bob",
            },
        ],
    )
    out: list[tuple[str, bytes, dict]] = []
    async for item in harvest_telegram_export(json_path=str(path)):
        out.append(item)
    # 2 days → 2 chunks.
    assert len(out) == 2
    by_date = {
        ctx["row_pk"]["date"]: (fname, blob, ctx) for fname, blob, ctx in out
    }
    assert "2026-05-29" in by_date
    assert "2026-05-30" in by_date

    fname, blob, ctx = by_date["2026-05-29"]
    body = blob.decode("utf-8")
    assert "Refund queue" in body
    assert "Alice" in body
    assert "Bob" in body
    assert ctx["table"] == "telegram_chat"
    assert ctx["row_pk"]["chat_id"] == "99887766"
    assert ctx["extras"]["chat_name"] == "Team chat"
    assert ctx["extras"]["message_count"] == 2
    assert ctx["extras"]["first_from"] == "Alice"
    assert fname.endswith("2026-05-29.txt")


@pytest.mark.asyncio
async def test_harvest_telegram_skips_service_messages(tmp_path: Path) -> None:
    path = _write_export(
        tmp_path,
        messages=[
            {"id": 1, "type": "service", "action": "join_group_by_link"},
            {
                "id": 2,
                "type": "message",
                "from": "Alice",
                "date": "2026-05-29T09:00:00",
                "text": "real message",
            },
        ],
    )
    out = []
    async for item in harvest_telegram_export(json_path=str(path)):
        out.append(item)
    assert len(out) == 1
    assert b"real message" in out[0][1]


@pytest.mark.asyncio
async def test_harvest_telegram_json_b64_path(tmp_path: Path) -> None:
    path = _write_export(
        tmp_path,
        messages=[
            {
                "id": 1, "type": "message", "from": "A",
                "date": "2026-05-29T09:00:00", "text": "x",
            }
        ],
    )
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    out = []
    async for item in harvest_telegram_export(json_b64=b64):
        out.append(item)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_harvest_telegram_requires_input() -> None:
    with pytest.raises(ValueError, match="json_path"):
        async for _ in harvest_telegram_export():
            pass


@pytest.mark.asyncio
async def test_harvest_telegram_bad_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        async for _ in harvest_telegram_export(json_path=str(p)):
            pass


@pytest.mark.asyncio
async def test_harvest_telegram_handles_run_list_text(tmp_path: Path) -> None:
    """When ``text`` is a list of formatted runs (bold/link/...),
    the harvester flattens them so the embedder sees plain prose."""
    path = _write_export(
        tmp_path,
        messages=[
            {
                "id": 1,
                "type": "message",
                "from": "Alice",
                "date": "2026-05-29T09:00:00",
                "text": [
                    {"type": "plain", "text": "Check "},
                    {"type": "link", "text": "this doc", "href": "https://x"},
                    {"type": "plain", "text": " before lunch"},
                ],
            }
        ],
    )
    out = []
    async for item in harvest_telegram_export(json_path=str(path)):
        out.append(item)
    body = out[0][1].decode("utf-8")
    assert "Check this doc before lunch" in body


@pytest.mark.asyncio
async def test_harvest_telegram_multilingual_preserved(tmp_path: Path) -> None:
    path = _write_export(
        tmp_path,
        messages=[
            {
                "id": 1,
                "type": "message",
                "from": "Алиса",
                "date": "2026-05-29T09:00:00",
                "text": "Salom! Какая дата возврата?",
            }
        ],
    )
    out = []
    async for item in harvest_telegram_export(json_path=str(path)):
        out.append(item)
    body = out[0][1].decode("utf-8")
    assert "Salom" in body
    assert "Какая дата" in body
    assert "Алиса" in body
