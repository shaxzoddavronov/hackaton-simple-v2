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


def test_single_row_with_dict_cell_coerces_to_string() -> None:
    """Regression: a JSONB cell that slipped through dtype detection as
    numeric (or whose dtype was misclassified) used to land in
    ``KPI.value`` as a dict, which then crashed React with
    "Objects are not valid as a React child". The KPI now stores a
    JSON-encoded string instead."""
    # Numeric dtype but the cell is actually a dict (e.g., Postgres
    # JSONB column wrongly typed). _coerce_to_primitive must catch it.
    spec = _pick_spec(
        _rs(
            ["score"],
            ["numeric"],
            [[{"value": 0.85}]],
        ),
        "what is the score",
    )
    assert spec.type == "kpi"
    # Dict gets JSON-stringified — never reaches the frontend as an object.
    assert isinstance(spec.value, str)
    assert "0.85" in spec.value


def test_single_row_with_decimal_cell_becomes_float() -> None:
    from decimal import Decimal

    spec = _pick_spec(
        _rs(["amount"], ["numeric"], [[Decimal("1234.56")]]),
        "amount",
    )
    assert spec.type == "kpi"
    assert isinstance(spec.value, float)
    assert spec.value == 1234.56


def test_single_row_with_dict_label_stringifies() -> None:
    """A composite-type column (asyncpg Record / Postgres ROW(...))
    landing in the label slot used to put the dict directly into the
    KPI label, which React then refused to render. After coercion it
    becomes a string."""
    spec = _pick_spec(
        _rs(
            ["meta", "count"],
            ["json", "int"],
            [[{"region": "EMEA"}, 42]],
        ),
        "events",
    )
    assert spec.type == "kpi"
    assert spec.value == 42
    # Whole label is a string — never carries an embedded object.
    assert isinstance(spec.label, str)
    assert "EMEA" in spec.label


def test_leaderboard_with_multiple_numerics_picks_grouped_bar() -> None:
    # When the planner returns multiple numeric columns alongside one
    # categorical anchor, render a GROUPED bar with every numeric as
    # its own series. The pre-Phase-34 rule mis-fell-through to
    # `table` here, which is what real users complained about
    # ("analitik grafik qilib korsatmadingku" — you didn't make a
    # chart!). All numerics travel together on y; incidental time
    # columns get filtered out of the y series but don't disqualify
    # the bar shape.
    spec = _pick_spec(
        _rs(
            ["display_name", "sessions_count", "correct_total", "last_activity"],
            ["text", "bigint", "bigint", "timestamp"],
            [
                ["Ali V.", 12, 80, "2026-05-20"],
                ["Bobur S.", 9, 65, "2026-05-25"],
            ],
        ),
        "eng faol foydalanuvchilarni grafik qilib korsat",
    )
    assert spec.type == "bar"
    assert spec.x == "display_name"
    # Both numerics show as separate series; timestamp excluded.
    assert spec.y == ["sessions_count", "correct_total"]
    assert "last_activity" not in spec.y
    assert len(spec.data) == 2


def test_two_numerics_plus_category_picks_grouped_bar() -> None:
    """The exact shape the user hit when asking for a registration
    chart: one categorical anchor + two numeric metrics + no time
    column. Old rule required ``nums == 1``, fell through to table."""
    spec = _pick_spec(
        _rs(
            ["display_name", "session_count", "attempt_count"],
            ["text", "bigint", "bigint"],
            [
                ["S.", 8, 0],
                ["Mashkura", 1, 1],
                ["Saodat", 1, 0],
            ],
        ),
        "foydalanuvchilarni royxatdan otishini grafik qilib korsat",
    )
    assert spec.type == "bar"
    assert spec.x == "display_name"
    assert spec.y == ["session_count", "attempt_count"]


def test_category_with_only_timestamp_still_picks_bar() -> None:
    """Edge case: category + numeric + timestamp. The category wins
    the X-axis; the timestamp gets dropped from y. Earlier rule
    refused bar whenever a time column was present."""
    spec = _pick_spec(
        _rs(
            ["region", "revenue", "last_sale"],
            ["text", "numeric", "timestamp"],
            [["EMEA", 100, "2026-05-30"], ["APAC", 80, "2026-05-31"]],
        ),
        "revenue by region",
    )
    assert spec.type == "bar"
    assert spec.x == "region"
    assert spec.y == ["revenue"]


def test_pie_only_when_single_metric_and_distribution_keyword() -> None:
    """Pie used to fire whenever the share/distribution keyword
    matched even if there were two numerics. Now strict: single
    numeric + small N + keyword."""
    # Two numerics with the distribution keyword → bar, NOT pie.
    spec = _pick_spec(
        _rs(
            ["region", "revenue", "orders"],
            ["text", "numeric", "bigint"],
            [["EMEA", 100, 5], ["APAC", 80, 3]],
        ),
        "show distribution by region",
    )
    assert spec.type == "bar"
