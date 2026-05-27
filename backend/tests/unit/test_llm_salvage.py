"""Unit tests for the JSON salvage helper in :mod:`app.agents.llm`.

These guarantee that the actual failure mode we saw in production —
``{"dialect": "postgres", ...}`` followed by thousands of trailing
newlines — gets repaired into a parseable JSON object before pydantic
sees it.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from app.agents.llm import _extract_first_json_object


class _Demo(BaseModel):
    dialect: str
    sql: str


def test_trims_trailing_newline_flood() -> None:
    payload = (
        '{"dialect": "postgres", "sql": "SELECT 1"}'
        + "\n" * 30000
    )
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)
    assert parsed["dialect"] == "postgres"
    assert _Demo.model_validate_json(out).sql == "SELECT 1"


def test_strips_markdown_fence() -> None:
    payload = '```json\n{"dialect":"sqlite","sql":"SELECT 2"}\n```\n'
    out = _extract_first_json_object(payload)
    assert _Demo.model_validate_json(out).dialect == "sqlite"


def test_strips_plain_fence() -> None:
    payload = '```\n{"dialect":"sqlite","sql":"SELECT 2"}\n```'
    out = _extract_first_json_object(payload)
    assert _Demo.model_validate_json(out).sql == "SELECT 2"


def test_extracts_object_after_prose_preamble() -> None:
    payload = (
        "Here is the SqlPlan you asked for:\n\n"
        '{"dialect":"postgres","sql":"SELECT 3"}\n'
        "Hope this helps!"
    )
    out = _extract_first_json_object(payload)
    assert _Demo.model_validate_json(out).sql == "SELECT 3"


def test_ignores_braces_inside_strings() -> None:
    # A '}' inside a string literal must not close the outer object.
    payload = '{"dialect":"postgres","sql":"SELECT \'a}b\' AS x"}'
    out = _extract_first_json_object(payload)
    parsed = _Demo.model_validate_json(out)
    assert "a}b" in parsed.sql


def test_unbalanced_input_gets_patched_to_parseable() -> None:
    """Salvager now patches unbalanced JSON instead of giving up.

    The pydantic validator still fails on this specific payload because
    ``dialect="postgres"`` is fine but the patched ``sql`` value will
    be ``"oh no"`` — _Demo demands ``sql: str`` so validation succeeds.
    The point of this test is that **parsing** no longer fails outright;
    schema-level errors (if any) are the only remaining failure mode.
    """
    payload = '   {"dialect":"postgres", "sql":"oh no'  # truncated mid-string
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)  # MUST parse
    assert parsed["dialect"] == "postgres"
    assert parsed["sql"] == "oh no"
    # And it round-trips through pydantic just fine.
    assert _Demo.model_validate_json(out).sql == "oh no"


def test_empty_input_passes_through() -> None:
    assert _extract_first_json_object("") == ""
    assert _extract_first_json_object("   \n\n  ") == ""


def test_nested_object_returns_outermost() -> None:
    payload = (
        '{"dialect":"postgres","sql":"SELECT 1","meta":{"a":1,"b":{"c":2}}}'
    )
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)
    assert parsed["meta"]["b"]["c"] == 2


# ── Truncation salvage — mid-string / mid-object ────────────────────


def test_truncated_mid_string_gets_closed() -> None:
    """Real failure pattern observed in prod: AnswerDraft cut off
    mid-body. The salvager should produce parseable JSON so the user
    sees a partial answer rather than a crash."""
    payload = (
        '{"headline":"Schema overview","body_md":"The DB has users and '
        'orders. Each user has many ord'
    )
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)  # MUST parse
    assert parsed["headline"] == "Schema overview"
    assert parsed["body_md"].startswith("The DB has users")


def test_truncated_mid_string_with_unicode() -> None:
    payload = (
        '{"headline":"Maʼlumot","body_md":"Foydalanuvchilar va ularning '
        'darajalari (student_analysis'
    )
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)
    assert "Foydalanuvchilar" in parsed["body_md"]


def test_truncated_inside_nested_object() -> None:
    payload = '{"a":1,"b":{"c":2,"d":{"e":3'
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)
    assert parsed["b"]["d"]["e"] == 3


def test_truncated_inside_array() -> None:
    payload = '{"items":["a","b","c'
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)
    assert parsed["items"] == ["a", "b", "c"]


def test_truncated_after_colon_inserts_null() -> None:
    payload = '{"key":'
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)
    assert parsed["key"] is None


def test_truncated_after_trailing_comma_dropped() -> None:
    payload = '{"a":1,"b":2,'
    out = _extract_first_json_object(payload)
    parsed = json.loads(out)
    assert parsed == {"a": 1, "b": 2}
