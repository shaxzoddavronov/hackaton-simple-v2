"""Phase 35 bug-fix — value-based dtype inference for schemaless engines.

Regression: postgres/sqlite/mongo/elasticsearch were returning blank or
"string" dtypes for every column, so the chart_designer rule engine
couldn't tell numeric from text and every chart shape fell to a table.
This helper is now the shared source of truth for the schemaless paths.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

from app.services.value_dtype import (
    infer_column_dtypes,
    infer_value_dtype,
)


# ── per-cell vocabulary ─────────────────────────────────────────


def test_int_to_bigint() -> None:
    assert infer_value_dtype(42) == "bigint"


def test_bool_is_not_int() -> None:
    # In Python bool is a subclass of int — we MUST pick bool first or
    # boolean columns wrongly classify as numeric.
    assert infer_value_dtype(True) == "bool"


def test_float_to_float8() -> None:
    assert infer_value_dtype(3.14) == "float8"


def test_decimal_to_numeric() -> None:
    assert infer_value_dtype(Decimal("99.99")) == "numeric"


def test_naive_datetime_is_timestamp() -> None:
    assert infer_value_dtype(datetime(2026, 1, 1, 12, 0)) == "timestamp"


def test_aware_datetime_is_timestamptz() -> None:
    dt = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert infer_value_dtype(dt) == "timestamptz"


def test_date_is_date() -> None:
    assert infer_value_dtype(date(2026, 1, 1)) == "date"


def test_time_is_time() -> None:
    assert infer_value_dtype(time(12, 30)) == "time"


def test_str_is_text() -> None:
    assert infer_value_dtype("hello") == "text"


def test_dict_is_jsonb() -> None:
    assert infer_value_dtype({"a": 1}) == "jsonb"


def test_list_is_jsonb() -> None:
    assert infer_value_dtype([1, 2, 3]) == "jsonb"


def test_bytes_is_bytea() -> None:
    assert infer_value_dtype(b"raw") == "bytea"


def test_none_returns_empty() -> None:
    assert infer_value_dtype(None) == ""


# ── per-column inference ────────────────────────────────────────


def test_infer_picks_first_nonnull_per_column() -> None:
    rows = [
        [None, None, None],
        [1, "Ali", datetime(2026, 1, 1)],
        [2, "Bobur", datetime(2026, 1, 2)],
    ]
    dtypes = infer_column_dtypes(["id", "name", "at"], rows)
    assert dtypes == ["bigint", "text", "timestamp"]


def test_infer_falls_back_to_text_when_all_null() -> None:
    rows = [[None, None], [None, None]]
    assert infer_column_dtypes(["a", "b"], rows) == ["text", "text"]


def test_infer_handles_empty_rows() -> None:
    assert infer_column_dtypes(["a", "b"], []) == ["text", "text"]


def test_infer_does_not_classify_bool_column_as_bigint() -> None:
    rows = [[True], [False], [True]]
    assert infer_column_dtypes(["active"], rows) == ["bool"]


def test_infer_respects_sample_window() -> None:
    # First 50 rows have None, row 51 has the real value. The default
    # sample window is 50, so we should NOT see it.
    rows = [[None] for _ in range(50)] + [["S."]]
    assert infer_column_dtypes(["name"], rows, sample=50) == ["text"]


def test_infer_yielding_user_chart_shape() -> None:
    """The exact shape the real user hit: display_name + 2 bigints
    that previously inferred as 'text' on every column."""
    rows = [
        ["S.", 8, 0],
        ["Mashkura", 1, 1],
        ["Saodat", 1, 0],
    ]
    dtypes = infer_column_dtypes(
        ["display_name", "session_count", "attempt_count"], rows
    )
    assert dtypes == ["text", "bigint", "bigint"]


def test_infer_yielding_leaderboard_shape() -> None:
    rows = [
        ["S.", 8, 30, 61, datetime(2026, 5, 7, 11, 15)],
        ["Saodat", 1, 14, 15, datetime(2026, 5, 6, 17, 23)],
    ]
    dtypes = infer_column_dtypes(
        ["display_name", "sessions_count", "correct_total",
         "questions_total", "last_activity"],
        rows,
    )
    assert dtypes == [
        "text", "bigint", "bigint", "bigint", "timestamp"
    ]
