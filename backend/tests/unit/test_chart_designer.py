"""Chart-designer rule tests.

Deterministic picks based on result shape, so we can lock the behaviour
in without an LLM round-trip. Replaces the previous LLM-driven variant
selection which always returned ``bar`` even for single-row results.
"""
from __future__ import annotations

import pytest

from app.agents.nodes.chart_designer import _pick_spec
from app.engines.base import ResultSet


def _rs(columns: list[str], dtypes: list[str], rows: list[list]) -> ResultSet:
    return ResultSet(
        columns=columns,
        dtypes=dtypes,
        rows=rows,
        row_count=len(rows),
        took_ms=0,
    )


def test_zero_rows_returns_text_only() -> None:
    spec = _pick_spec(_rs(["x"], ["int"], []), "anything")
    assert spec.type == "text_only"


def test_single_row_single_numeric_is_kpi() -> None:
    spec = _pick_spec(
        _rs(["sessions_count"], ["bigint"], [[8]]),
        "Eng faol foydalanuvchi kim",
    )
    assert spec.type == "kpi"
    assert spec.value == 8


def test_single_row_with_label_pairs_label_into_kpi() -> None:
    spec = _pick_spec(
        _rs(
            ["full_name", "sessions_count"],
            ["text", "bigint"],
            [["S.", 8]],
        ),
        "Eng faol foydalanuvchi",
    )
    assert spec.type == "kpi"
    assert spec.value == 8
    # Label includes both column name + the row's label value
    assert "S." in spec.label


def test_multi_row_category_and_numeric_picks_bar() -> None:
    spec = _pick_spec(
        _rs(
            ["region", "revenue"],
            ["text", "numeric"],
            [["EMEA", 100], ["APAC", 80], ["AMER", 60]],
        ),
        "revenue by region",
    )
    assert spec.type == "bar"
    assert spec.x == "region"
    assert spec.y == ["revenue"]
    assert len(spec.data) == 3


def test_multi_row_time_and_numeric_picks_line() -> None:
    spec = _pick_spec(
        _rs(
            ["ts", "amount"],
            ["timestamp", "numeric"],
            [["2026-01-01", 10], ["2026-01-02", 12], ["2026-01-03", 15]],
        ),
        "revenue trend",
    )
    assert spec.type == "line"
    assert spec.x == "ts"


def test_distribution_keyword_triggers_pie_for_small_n() -> None:
    spec = _pick_spec(
        _rs(
            ["region", "share"],
            ["text", "numeric"],
            [["EMEA", 0.5], ["APAC", 0.3], ["AMER", 0.2]],
        ),
        "show distribution by region",
    )
    assert spec.type == "pie"


def test_many_columns_falls_back_to_table() -> None:
    spec = _pick_spec(
        _rs(
            ["a", "b", "c", "d"],
            ["text", "int", "text", "numeric"],
            [["x", 1, "y", 1.5], ["z", 2, "w", 2.5]],
        ),
        "anything",
    )
    assert spec.type == "table"
    assert len(spec.columns) == 4


def test_leaderboard_with_extras_picks_table() -> None:
    # When planner returns multiple numeric columns alongside the name,
    # bar isn't a good fit — fall back to table so all metrics show.
    spec = _pick_spec(
        _rs(
            ["display_name", "sessions_count", "correct_total", "last_activity"],
            ["text", "bigint", "bigint", "timestamp"],
            [
                ["Ali V.", 12, 80, "2026-05-20"],
                ["Bobur S.", 9, 65, "2026-05-25"],
            ],
        ),
        "Eng faol foydalanuvchilar",
    )
    assert spec.type == "table"
