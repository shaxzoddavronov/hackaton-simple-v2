"""Pure-Python merge engine spec.

Covers the join/union/concat primitives plus the merge-pipeline
driver. No I/O, no DB, no LLM.
"""
from __future__ import annotations

import pytest

from app.engines.base import ResultSet
from app.services.federation_merge import (
    MergeError,
    execute_merge_pipeline,
    merge_concat,
    merge_join,
    merge_union,
)


def _rs(columns: list[str], rows: list[list]) -> ResultSet:
    return ResultSet(
        columns=columns,
        dtypes=["string"] * len(columns),
        rows=rows,
        row_count=len(rows),
        took_ms=0,
    )


# ── merge_join ──────────────────────────────────────────────────────


def test_join_inner_one_key() -> None:
    left = _rs(["user_id", "revenue"], [[1, 100], [2, 50], [3, 75]])
    right = _rs(["user_id", "country"], [[1, "UZ"], [2, "US"]])
    out = merge_join(left, right, on=["user_id"])
    assert out.columns == ["user_id", "revenue", "country"]
    # Only matching keys survive — user 3 has no country row.
    assert sorted(out.rows) == [[1, 100, "UZ"], [2, 50, "US"]]
    assert out.row_count == 2


def test_join_one_to_many_emits_cartesian_per_key() -> None:
    left = _rs(["user_id", "revenue"], [[1, 100]])
    right = _rs(
        ["user_id", "order_id"], [[1, "a"], [1, "b"], [1, "c"]]
    )
    out = merge_join(left, right, on=["user_id"])
    assert out.row_count == 3


def test_join_no_match_returns_empty() -> None:
    left = _rs(["k", "v"], [[1, "x"]])
    right = _rs(["k", "y"], [[2, "z"]])
    out = merge_join(left, right, on=["k"])
    assert out.rows == []
    assert out.row_count == 0


def test_join_rejects_empty_on() -> None:
    with pytest.raises(MergeError):
        merge_join(_rs(["k"], []), _rs(["k"], []), on=[])


def test_join_rejects_missing_key() -> None:
    with pytest.raises(MergeError):
        merge_join(
            _rs(["a"], [[1]]), _rs(["b"], [[1]]), on=["k"]
        )


def test_join_composite_key() -> None:
    left = _rs(["region", "year", "rev"], [["EU", 2024, 10], ["EU", 2025, 20]])
    right = _rs(
        ["region", "year", "growth"],
        [["EU", 2024, 0.1], ["EU", 2025, 0.2], ["US", 2024, 0.5]],
    )
    out = merge_join(left, right, on=["region", "year"])
    assert out.columns == ["region", "year", "rev", "growth"]
    assert sorted(out.rows) == [["EU", 2024, 10, 0.1], ["EU", 2025, 20, 0.2]]


# ── merge_union ─────────────────────────────────────────────────────


def test_union_dedupes_identical_rows() -> None:
    left = _rs(["k", "v"], [[1, "a"], [2, "b"]])
    right = _rs(["k", "v"], [[2, "b"], [3, "c"]])
    out = merge_union(left, right)
    assert out.columns == ["k", "v"]
    assert sorted(out.rows) == [[1, "a"], [2, "b"], [3, "c"]]


def test_union_handles_reordered_columns() -> None:
    left = _rs(["k", "v"], [[1, "a"]])
    right = _rs(["v", "k"], [["a", 1], ["b", 2]])  # same set, different order
    out = merge_union(left, right)
    assert out.columns == ["k", "v"]
    # Both 'a/1' rows dedup; only [2, "b"] is fresh.
    assert sorted(out.rows) == [[1, "a"], [2, "b"]]


def test_union_rejects_mismatched_columns() -> None:
    with pytest.raises(MergeError):
        merge_union(_rs(["a"], [[1]]), _rs(["b"], [[2]]))


# ── merge_concat ────────────────────────────────────────────────────


def test_concat_stacks_with_column_union() -> None:
    left = _rs(["a", "b"], [[1, 2], [3, 4]])
    right = _rs(["a", "c"], [[5, 6], [7, 8]])
    out = merge_concat(left, right)
    assert out.columns == ["a", "b", "c"]
    assert out.rows == [
        [1, 2, None],
        [3, 4, None],
        [5, None, 6],
        [7, None, 8],
    ]


def test_concat_no_dedup() -> None:
    left = _rs(["k"], [[1]])
    right = _rs(["k"], [[1]])
    out = merge_concat(left, right)
    assert out.rows == [[1], [1]]


# ── execute_merge_pipeline ──────────────────────────────────────────


def test_pipeline_single_subquery_no_steps() -> None:
    sole = _rs(["x"], [[1]])
    out = execute_merge_pipeline({"only": sole}, steps=[])
    assert out is sole


def test_pipeline_chains_joins() -> None:
    a = _rs(["user_id", "revenue"], [[1, 100], [2, 50]])
    b = _rs(["user_id", "country"], [[1, "UZ"], [2, "US"]])
    c = _rs(["country", "currency"], [["UZ", "UZS"], ["US", "USD"]])
    out = execute_merge_pipeline(
        {"a": a, "b": b, "c": c},
        steps=[
            {"kind": "join", "left": "a", "right": "b", "on": ["user_id"], "output": "ab"},
            {"kind": "join", "left": "ab", "right": "c", "on": ["country"], "output": "final"},
        ],
    )
    assert out.columns == ["user_id", "revenue", "country", "currency"]
    assert sorted(out.rows) == [[1, 100, "UZ", "UZS"], [2, 50, "US", "USD"]]


def test_pipeline_rejects_unknown_alias() -> None:
    with pytest.raises(MergeError):
        execute_merge_pipeline(
            {"a": _rs(["k"], [])},
            steps=[
                {"kind": "join", "left": "a", "right": "ghost", "on": ["k"], "output": "x"}
            ],
        )


def test_pipeline_ambiguous_multiple_subresults_no_steps() -> None:
    # Without merge steps, the agent must hand us exactly one result.
    with pytest.raises(MergeError):
        execute_merge_pipeline(
            {"a": _rs(["x"], []), "b": _rs(["y"], [])},
            steps=[],
        )
