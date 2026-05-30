"""Phase 26 — dashboards + saved-questions API.

The route handlers are thin SQLAlchemy CRUD. We test the Pydantic
contracts and the helper functions directly; full HTTP roundtrip
testing is left to the e2e Postgres fixture (which the unit suite
skips when no DB is running).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.api.dashboards import (
    DashboardCreate,
    DashboardOut,
    SavedQuestionCreate,
    SavedQuestionOut,
    SavedQuestionUpdate,
    _dash_out,
    _sq_out,
)


# ── DashboardCreate ─────────────────────────────────────────────


def test_dashboard_create_requires_name() -> None:
    with pytest.raises(Exception):
        DashboardCreate(name="")


def test_dashboard_create_name_capped() -> None:
    # Pydantic enforces max_length=255 on the field.
    with pytest.raises(Exception):
        DashboardCreate(name="a" * 256)


def test_dashboard_create_description_optional() -> None:
    d = DashboardCreate(name="ops")
    assert d.description is None


def test_dashboard_create_rejects_unknown_fields() -> None:
    with pytest.raises(Exception):
        DashboardCreate(name="ops", surprise="boom")  # type: ignore[call-arg]


# ── SavedQuestionCreate ─────────────────────────────────────────


def test_saved_question_minimal() -> None:
    sq = SavedQuestionCreate(
        title="refund queue", prompt="how many refunds today?"
    )
    assert sq.dashboard_id is None
    assert sq.connection_id is None


def test_saved_question_with_dashboard() -> None:
    did = uuid4()
    cid = uuid4()
    sq = SavedQuestionCreate(
        title="t",
        prompt="p",
        dashboard_id=did,
        connection_id=cid,
    )
    assert sq.dashboard_id == did
    assert sq.connection_id == cid


def test_saved_question_title_required() -> None:
    with pytest.raises(Exception):
        SavedQuestionCreate(title="", prompt="p")


def test_saved_question_prompt_required() -> None:
    with pytest.raises(Exception):
        SavedQuestionCreate(title="t", prompt="")


def test_saved_question_prompt_capped() -> None:
    with pytest.raises(Exception):
        SavedQuestionCreate(title="t", prompt="x" * 4001)


def test_saved_question_update_partial() -> None:
    upd = SavedQuestionUpdate(title="renamed")
    assert upd.title == "renamed"
    assert upd.dashboard_id is None
    assert upd.position is None


def test_saved_question_update_rejects_unknown_field() -> None:
    with pytest.raises(Exception):
        SavedQuestionUpdate(category="ops")  # type: ignore[call-arg]


# ── _sq_out / _dash_out shape helpers ───────────────────────────


def _fake_dashboard(name: str = "ops"):
    """Lightweight stand-in matching the SavedQuestion / Dashboard
    attribute access pattern. Real ORM round-trip is covered by the
    e2e fixture."""

    class _D:
        id = uuid4()
        owner_id = uuid4()
        workspace_id = uuid4()
        description: str | None = None
        created_at = datetime.now(timezone.utc)
        updated_at = datetime.now(timezone.utc)

    d = _D()
    d.name = name
    return d


def _fake_saved_question(*, dashboard_id=None, connection_id=None):
    class _Q:
        id = uuid4()
        owner_id = uuid4()
        workspace_id = uuid4()
        title = "refund queue today"
        prompt = "How many refunds did we process today?"
        position = None
        created_at = datetime.now(timezone.utc)

    q = _Q()
    q.dashboard_id = dashboard_id
    q.connection_id = connection_id
    return q


def test_dash_out_carries_question_count() -> None:
    d = _fake_dashboard("ops")
    out = _dash_out(d, question_count=5)
    assert isinstance(out, DashboardOut)
    assert out.question_count == 5
    assert out.name == "ops"


def test_sq_out_stringifies_uuids() -> None:
    did = uuid4()
    cid = uuid4()
    q = _fake_saved_question(dashboard_id=did, connection_id=cid)
    out = _sq_out(q)
    assert isinstance(out, SavedQuestionOut)
    assert out.dashboard_id == str(did)
    assert out.connection_id == str(cid)
    assert out.title == "refund queue today"


def test_sq_out_handles_null_dashboard_and_connection() -> None:
    q = _fake_saved_question()
    out = _sq_out(q)
    assert out.dashboard_id is None
    assert out.connection_id is None
