"""Phase 21 — Slack workspace export ZIP harvester.

We synthesise a minimal valid Slack export inside a tmp_path and run
the real harvester against it (no mocking of zipfile / json — the
parser logic is what we care about).
"""
from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.services.doc_harvest import (
    _format_slack_thread,
    _group_slack_threads,
    harvest_slack_export,
)


# ── thread grouping ──────────────────────────────────────────────


def test_group_threads_parent_and_replies() -> None:
    msgs = [
        {"ts": "100.000", "text": "parent A", "user": "U1"},
        {"ts": "101.000", "thread_ts": "100.000", "text": "reply A1", "user": "U2"},
        {"ts": "102.000", "thread_ts": "100.000", "text": "reply A2", "user": "U1"},
        {"ts": "200.000", "text": "standalone B", "user": "U3"},
    ]
    threads = _group_slack_threads(msgs)
    assert set(threads.keys()) == {"100.000", "200.000"}
    assert len(threads["100.000"]) == 3
    # Parent first, replies after, sorted by ts ascending.
    assert threads["100.000"][0]["text"] == "parent A"
    assert threads["100.000"][-1]["text"] == "reply A2"
    assert len(threads["200.000"]) == 1


def test_group_threads_skips_system_messages() -> None:
    msgs = [
        {"ts": "1.000", "text": "joined channel", "subtype": "channel_join"},
        {"ts": "2.000", "text": "real message", "user": "U1"},
        {"ts": "3.000", "text": "name changed", "subtype": "channel_name"},
    ]
    threads = _group_slack_threads(msgs)
    assert set(threads.keys()) == {"2.000"}


def test_group_threads_ignores_malformed_entries() -> None:
    msgs = [
        "not-a-dict",  # type: ignore[list-item]
        {"no_ts": "bad"},
        {"ts": "5.000", "text": "good", "user": "U1"},
    ]
    threads = _group_slack_threads(msgs)  # type: ignore[arg-type]
    assert set(threads.keys()) == {"5.000"}


# ── thread formatting ────────────────────────────────────────────


def test_format_thread_resolves_user_names() -> None:
    thread = [
        {"ts": "1700000000.000", "text": "hi", "user": "U1"},
        {"ts": "1700000001.000", "text": "hello", "user": "U2"},
    ]
    users = {"U1": "Alice Anderson", "U2": "Bob Brown"}
    text, first_user, first_date = _format_slack_thread(thread, users)
    assert "Alice Anderson" in text
    assert "Bob Brown" in text
    assert first_user == "Alice Anderson"
    # ISO 8601 date for ts=1700000000.000 → 2023-11-14T...Z
    assert first_date.startswith("2023-11-14")


def test_format_thread_lists_attachment_names() -> None:
    thread = [
        {
            "ts": "1700000000.000",
            "text": "see attached",
            "user": "U1",
            "files": [
                {"name": "policy.pdf", "mimetype": "application/pdf"},
                {"name": "diagram.png", "mimetype": "image/png"},
            ],
        }
    ]
    text, _, _ = _format_slack_thread(thread, {"U1": "Alice"})
    assert "policy.pdf" in text
    assert "diagram.png" in text


def test_format_thread_falls_back_to_user_id_when_unknown() -> None:
    thread = [{"ts": "1.000", "text": "x", "user": "U_UNKNOWN"}]
    text, first_user, _ = _format_slack_thread(thread, {})
    assert "U_UNKNOWN" in text
    assert first_user == "U_UNKNOWN"


# ── harvester end-to-end with a synthesised export ZIP ───────────


def _build_slack_export_zip(tmp_path: Path) -> Path:
    """Build a minimal Slack export ZIP fixture and return its path."""
    zip_path = tmp_path / "slack_export.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "users.json",
            json.dumps(
                [
                    {
                        "id": "U1",
                        "name": "alice",
                        "profile": {"real_name": "Alice Anderson"},
                    },
                    {
                        "id": "U2",
                        "name": "bob",
                        "profile": {"real_name": "Bob Brown"},
                    },
                ]
            ),
        )
        zf.writestr(
            "channels.json",
            json.dumps([{"id": "C1", "name": "engineering"}]),
        )
        # Day 1 of engineering channel
        zf.writestr(
            "engineering/2026-05-29.json",
            json.dumps(
                [
                    {
                        "ts": "1748509200.000100",
                        "text": "Should we merge the rebase?",
                        "user": "U1",
                    },
                    {
                        "ts": "1748509260.000100",
                        "thread_ts": "1748509200.000100",
                        "text": "LGTM",
                        "user": "U2",
                    },
                ]
            ),
        )
        # Day 2 — separate thread
        zf.writestr(
            "engineering/2026-05-30.json",
            json.dumps(
                [
                    {
                        "ts": "1748595600.000100",
                        "text": "Refund policy update reminder",
                        "user": "U1",
                    }
                ]
            ),
        )
        # Channel-system noise that should be skipped
        zf.writestr(
            "general/2026-05-30.json",
            json.dumps(
                [
                    {
                        "ts": "1748600000.0",
                        "subtype": "channel_join",
                        "text": "Alice joined",
                    }
                ]
            ),
        )
    return zip_path


@pytest.mark.asyncio
async def test_harvest_slack_export_zip_path_yields_threads(
    tmp_path: Path,
) -> None:
    zip_path = _build_slack_export_zip(tmp_path)
    out: list[tuple[str, bytes, dict]] = []
    async for item in harvest_slack_export(zip_path=str(zip_path)):
        out.append(item)

    # 2 threads in engineering channel; general channel had only a
    # system message so nothing yields from there.
    assert len(out) == 2

    by_channel: dict[str, list] = {}
    for fname, blob, ctx in out:
        by_channel.setdefault(ctx["row_pk"]["channel"], []).append(
            (fname, blob, ctx)
        )

    assert "engineering" in by_channel
    assert "general" not in by_channel

    # Find the rebase thread + verify reply is attached.
    rebase_thread = next(
        item for item in by_channel["engineering"]
        if "rebase" in item[1].decode("utf-8")
    )
    fname, blob, ctx = rebase_thread
    text = blob.decode("utf-8")
    assert "Alice Anderson" in text
    assert "LGTM" in text  # reply preserved
    assert "Bob Brown" in text  # reply author
    assert ctx["table"] == "slack_thread"
    assert ctx["row_pk"]["channel"] == "engineering"
    assert ctx["extras"]["message_count"] == 2
    assert ctx["extras"]["reply_count"] == 1
    assert ctx["extras"]["first_user"] == "Alice Anderson"
    assert ctx["connection_id"].startswith("slack:")
    assert fname.startswith("slack_engineering_")


@pytest.mark.asyncio
async def test_harvest_slack_export_zip_b64(tmp_path: Path) -> None:
    """The upload path: bytes are base64-encoded and passed via
    ``zip_b64``."""
    zip_path = _build_slack_export_zip(tmp_path)
    b64 = base64.b64encode(zip_path.read_bytes()).decode("ascii")
    out = []
    async for item in harvest_slack_export(zip_b64=b64):
        out.append(item)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_harvest_slack_only_channels_filter(tmp_path: Path) -> None:
    zip_path = _build_slack_export_zip(tmp_path)
    out = []
    async for item in harvest_slack_export(
        zip_path=str(zip_path),
        only_channels=["does_not_exist"],
    ):
        out.append(item)
    assert out == []


@pytest.mark.asyncio
async def test_harvest_slack_requires_zip_input() -> None:
    with pytest.raises(ValueError, match="zip_path"):
        async for _ in harvest_slack_export():
            pass


@pytest.mark.asyncio
async def test_harvest_slack_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        async for _ in harvest_slack_export(
            zip_path=str(tmp_path / "missing.zip")
        ):
            pass


@pytest.mark.asyncio
async def test_harvest_slack_multilingual_threads_preserved(
    tmp_path: Path,
) -> None:
    """Cyrillic / Uzbek-Latin text must round-trip through the ZIP +
    JSON pipeline intact so bge-m3 can embed them cross-lingually."""
    zp = tmp_path / "multi.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr(
            "users.json",
            json.dumps(
                [{"id": "U1", "profile": {"real_name": "Алиса"}}]
            ),
        )
        zf.writestr(
            "general/2026-01-01.json",
            json.dumps(
                [
                    {
                        "ts": "1735689600.0",
                        "text": "Salom! Какая дата возврата?",
                        "user": "U1",
                    }
                ]
            ),
        )
    out = []
    async for item in harvest_slack_export(zip_path=str(zp)):
        out.append(item)
    assert len(out) == 1
    body = out[0][1].decode("utf-8")
    assert "Salom" in body
    assert "Какая дата" in body
    assert "Алиса" in body
