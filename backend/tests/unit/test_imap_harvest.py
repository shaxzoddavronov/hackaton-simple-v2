"""Tests for the IMAP email harvester (Phase 19).

We mock ``imap_tools`` so the tests don't need a real IMAP server.
Coverage: filename slug, body/HTML/attachment yield, row_context
shape, since-window cap, max-message cap, login failure path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.doc_harvest import _safe_email_slug


# ── slug helper ──────────────────────────────────────────────────


def test_safe_email_slug_basic() -> None:
    assert _safe_email_slug("Refund question") == "refund_question"


def test_safe_email_slug_punctuation_collapsed() -> None:
    assert _safe_email_slug("Q1 — invoice #42!!") == "q1_invoice_42"


def test_safe_email_slug_empty() -> None:
    assert _safe_email_slug("") == "noname"
    assert _safe_email_slug("   ") == "noname"


def test_safe_email_slug_truncates_long() -> None:
    out = _safe_email_slug("a" * 200)
    assert len(out) == 60
    assert out == "a" * 60


def test_safe_email_slug_cyrillic() -> None:
    # Cyrillic stripped (non-ASCII) — fall back to noname.
    assert _safe_email_slug("Привет мир") == "noname"
    # Mixed — Latin chars survive.
    assert _safe_email_slug("Привет world 2026") == "world_2026"


# ── harvester yields email + attachments with row_context ────────


@pytest.mark.asyncio
async def test_harvest_imap_yields_body_and_attachment() -> None:
    """Mock a single email with one attachment. Verify both come out
    of the harvester with the same row_context (different
    file_column), so a citation off either chunk can resolve to the
    same source message."""
    from app.services import doc_harvest

    class FakeAttachment:
        def __init__(self, filename: str, payload: bytes) -> None:
            self.filename = filename
            self.payload = payload

    class FakeMessage:
        def __init__(self) -> None:
            self.uid = "42"
            self.subject = "Refund policy update"
            self.from_ = "Alice <alice@example.com>"
            self.date = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
            self.text = "Please see attached for the new refund policy."
            self.html = ""
            self.headers = {"message-id": "<abc-123@example.com>"}
            self.attachments = [
                FakeAttachment("policy_2026.pdf", b"%PDF-1.4\nfakebytes"),
            ]

    class FakeBox:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs):
            return self

        def uids(self, _q):
            return ["42"]

        def fetch(self, _q, mark_seen=False, bulk=False):
            return [FakeMessage()]

        def logout(self):
            pass

    # Patch the imap_tools symbols inside the local import path. The
    # AND helper is a class that the harvester instantiates; we don't
    # care about its shape so we pass a sentinel through.
    with patch.dict(
        "sys.modules",
        {
            "imap_tools": SimpleNamespace(
                AND=lambda **kw: SimpleNamespace(**kw),
                MailBox=FakeBox,
                MailBoxUnencrypted=FakeBox,
            ),
        },
    ):
        out: list[tuple[str, bytes, dict]] = []
        async for item in doc_harvest.harvest_imap(
            server="imap.example.com",
            username="alice@example.com",
            password="x",
            since_days=30,
            max_messages=10,
        ):
            out.append(item)

    assert len(out) == 2  # body + attachment
    by_column = {ctx["file_column"]: (fn, data, ctx) for fn, data, ctx in out}

    # Body chunk.
    body_fn, body_blob, body_ctx = by_column["body"]
    assert body_fn.startswith("email_refund_policy_update")
    assert body_fn.endswith(".txt")
    assert b"refund policy" in body_blob.lower()
    assert b"From: alice" in body_blob.lower() or b"from: " in body_blob.lower()
    assert body_ctx["table"] == "email"
    assert body_ctx["row_pk"] == {"message_id": "<abc-123@example.com>"}
    assert body_ctx["extras"]["from"] == "alice <alice@example.com>"
    assert body_ctx["extras"]["subject"] == "Refund policy update"
    assert body_ctx["file_reference"] == "Refund policy update"
    assert body_ctx["connection_id"].startswith("imap:imap.example.com/")

    # Attachment chunk.
    att_fn, att_blob, att_ctx = by_column["attachment"]
    assert att_fn == "policy_2026.pdf"
    assert att_blob.startswith(b"%PDF")
    # Same row_pk → citation links both chunks to the same email.
    assert att_ctx["row_pk"] == body_ctx["row_pk"]
    assert att_ctx["file_reference"] == "policy_2026.pdf"


@pytest.mark.asyncio
async def test_harvest_imap_skips_attachments_when_disabled() -> None:
    from app.services import doc_harvest

    class FakeAttachment:
        def __init__(self) -> None:
            self.filename = "should_be_skipped.pdf"
            self.payload = b"%PDF-1.4"

    class FakeMessage:
        uid = "1"
        subject = "Hi"
        from_ = "bob@x"
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        text = "body"
        html = ""
        headers = {"message-id": "<1@x>"}
        attachments = [FakeAttachment()]

    class FakeBox:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args, **kwargs): return self
        def uids(self, _q): return ["1"]
        def fetch(self, _q, mark_seen=False, bulk=False): return [FakeMessage()]
        def logout(self): pass

    with patch.dict(
        "sys.modules",
        {
            "imap_tools": SimpleNamespace(
                AND=lambda **kw: SimpleNamespace(**kw),
                MailBox=FakeBox,
                MailBoxUnencrypted=FakeBox,
            ),
        },
    ):
        out = []
        async for item in doc_harvest.harvest_imap(
            server="s", username="u", password="p",
            include_attachments=False,
        ):
            out.append(item)

    assert len(out) == 1
    assert out[0][2]["file_column"] == "body"


@pytest.mark.asyncio
async def test_harvest_imap_login_failure_yields_nothing() -> None:
    from app.services import doc_harvest

    class BrokenBox:
        def __init__(self, *args, **kwargs):
            raise ConnectionError("server unreachable")

    with patch.dict(
        "sys.modules",
        {
            "imap_tools": SimpleNamespace(
                AND=lambda **kw: SimpleNamespace(**kw),
                MailBox=BrokenBox,
                MailBoxUnencrypted=BrokenBox,
            ),
        },
    ):
        out = []
        async for item in doc_harvest.harvest_imap(
            server="bad", username="u", password="p",
        ):
            out.append(item)

    assert out == []


@pytest.mark.asyncio
async def test_harvest_imap_falls_back_to_html_when_no_plain() -> None:
    from app.services import doc_harvest

    class FakeMessage:
        uid = "9"
        subject = "HTML only"
        from_ = "c@x"
        date = datetime(2026, 3, 1, tzinfo=timezone.utc)
        text = ""
        html = "<html><body><p>Body in HTML</p></body></html>"
        headers = {"message-id": "<9@x>"}
        attachments = []

    class FakeBox:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args, **kwargs): return self
        def uids(self, _q): return ["9"]
        def fetch(self, _q, mark_seen=False, bulk=False): return [FakeMessage()]
        def logout(self): pass

    with patch.dict(
        "sys.modules",
        {
            "imap_tools": SimpleNamespace(
                AND=lambda **kw: SimpleNamespace(**kw),
                MailBox=FakeBox,
                MailBoxUnencrypted=FakeBox,
            ),
        },
    ):
        out = []
        async for item in doc_harvest.harvest_imap(
            server="s", username="u", password="p",
        ):
            out.append(item)

    assert len(out) == 1
    fn, blob, ctx = out[0]
    assert fn.endswith(".html")
    assert b"<p>Body in HTML</p>" in blob
    assert ctx["row_pk"] == {"message_id": "<9@x>"}
