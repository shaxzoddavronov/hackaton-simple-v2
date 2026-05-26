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


def test_unbalanced_input_returns_trimmed_text() -> None:
    payload = '   {"dialect":"postgres", "sql":"oh no'  # no closing brace
    out = _extract_first_json_object(payload)
    # Salvage gives up gracefully; caller's pydantic will fail with a clear msg.
    with pytest.raises((ValidationError, ValueError)):
        _Demo.model_validate_json(out)


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
