"""Pure-Python merge for federated query results.

When the agent decomposes a question into N sub-queries (one per
connection), each sub-query returns a :class:`ResultSet`. This module
combines them — inner equijoin, union, or concat — without pulling in
pandas. The implementation is straightforward: hash-join for ``join``,
set-dedup for ``union``, column-aligned stack for ``concat``.

The merge engine knows nothing about dialects or LLMs. It operates on
the shared :class:`ResultSet` shape so the same code merges results
from Postgres, ClickHouse, Mongo, or Elasticsearch.
"""
from __future__ import annotations

from typing import Any

from app.engines.base import ResultSet


class MergeError(ValueError):
    """Raised when a merge step references an unknown alias or breaks
    its own contract (e.g., union of mismatched columns)."""


def merge_join(
    left: ResultSet,
    right: ResultSet,
    on: list[str],
) -> ResultSet:
    """Inner equijoin over the ``on`` columns.

    Output columns = left.columns + (right.columns minus on).
    """
    if not on:
        raise MergeError("'join' requires at least one column in `on`")
    for c in on:
        if c not in left.columns:
            raise MergeError(f"join key {c!r} missing on left side")
        if c not in right.columns:
            raise MergeError(f"join key {c!r} missing on right side")

    l_idx = [left.columns.index(c) for c in on]
    r_idx = [right.columns.index(c) for c in on]

    right_extra_cols = [c for c in right.columns if c not in on]
    right_extra_idx = [right.columns.index(c) for c in right_extra_cols]
    out_cols = list(left.columns) + right_extra_cols

    # Hash the smaller side for memory friendliness on big asymmetric
    # joins. We hash the right for simplicity here; could pick the
    # smaller at runtime if it matters.
    index: dict[tuple, list[list[Any]]] = {}
    for row in right.rows:
        k = tuple(row[i] for i in r_idx)
        index.setdefault(k, []).append(row)

    out_rows: list[list[Any]] = []
    for lrow in left.rows:
        k = tuple(lrow[i] for i in l_idx)
        matches = index.get(k)
        if not matches:
            continue
        for rrow in matches:
            out_rows.append(list(lrow) + [rrow[i] for i in right_extra_idx])

    out_dtypes = list(left.dtypes) + [right.dtypes[i] for i in right_extra_idx]
    return ResultSet(
        columns=out_cols,
        dtypes=out_dtypes,
        rows=out_rows,
        row_count=len(out_rows),
        truncated=left.truncated or right.truncated,
        took_ms=left.took_ms + right.took_ms,
    )


def merge_union(left: ResultSet, right: ResultSet) -> ResultSet:
    """Vertical stack with duplicate-row removal.

    Requires identical column sets (order-insensitive). We project both
    sides into the left's column order for output.
    """
    if set(left.columns) != set(right.columns):
        raise MergeError(
            "'union' requires identical column sets; "
            f"left={left.columns}, right={right.columns}"
        )
    out_cols = list(left.columns)
    r_proj_idx = [right.columns.index(c) for c in out_cols]

    seen: set[tuple] = set()
    out_rows: list[list[Any]] = []
    for row in left.rows:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        out_rows.append(list(row))
    for row in right.rows:
        proj = [row[i] for i in r_proj_idx]
        key = tuple(proj)
        if key in seen:
            continue
        seen.add(key)
        out_rows.append(proj)

    return ResultSet(
        columns=out_cols,
        dtypes=list(left.dtypes),
        rows=out_rows,
        row_count=len(out_rows),
        truncated=left.truncated or right.truncated,
        took_ms=left.took_ms + right.took_ms,
    )


def merge_concat(left: ResultSet, right: ResultSet) -> ResultSet:
    """Vertical stack with column union (no dedup).

    Columns absent from one side are filled with ``None`` in that side's
    rows. Useful for "list everything from both connections" answers.
    """
    out_cols: list[str] = list(left.columns)
    for c in right.columns:
        if c not in out_cols:
            out_cols.append(c)

    def _project(rs: ResultSet) -> list[list[Any]]:
        idx = {c: rs.columns.index(c) for c in rs.columns}
        out: list[list[Any]] = []
        for row in rs.rows:
            out.append([row[idx[c]] if c in idx else None for c in out_cols])
        return out

    out_rows = _project(left) + _project(right)

    # dtype merge: prefer left's dtype for shared columns, else right's.
    out_dtypes: list[str] = []
    for c in out_cols:
        if c in left.columns:
            out_dtypes.append(left.dtypes[left.columns.index(c)])
        else:
            out_dtypes.append(right.dtypes[right.columns.index(c)])

    return ResultSet(
        columns=out_cols,
        dtypes=out_dtypes,
        rows=out_rows,
        row_count=len(out_rows),
        truncated=left.truncated or right.truncated,
        took_ms=left.took_ms + right.took_ms,
    )


def execute_merge_pipeline(
    sub_results: dict[str, ResultSet],
    steps: list[dict[str, Any]],
) -> ResultSet:
    """Run the merge_steps in order and return the final ResultSet.

    ``sub_results`` maps sub-query aliases → results. ``steps`` is a
    list of dicts with the shape of :class:`schemas.llm_io.MergeStep`.
    Each step's ``output`` alias is registered into ``sub_results`` so
    later steps can reference it.

    If ``steps`` is empty, returns the SOLE entry in ``sub_results``
    (typical for single-connection plans that ended up routed through
    the federated path by mistake).
    """
    if not steps:
        if len(sub_results) != 1:
            raise MergeError(
                "no merge steps but multiple sub_results — plan is ambiguous"
            )
        return next(iter(sub_results.values()))

    last_alias: str | None = None
    for i, step in enumerate(steps):
        kind = step["kind"]
        left_alias = step["left"]
        right_alias = step["right"]
        out_alias = step["output"]
        if left_alias not in sub_results:
            raise MergeError(f"step {i}: unknown left alias {left_alias!r}")
        if right_alias not in sub_results:
            raise MergeError(f"step {i}: unknown right alias {right_alias!r}")
        left = sub_results[left_alias]
        right = sub_results[right_alias]
        if kind == "join":
            merged = merge_join(left, right, on=list(step.get("on") or []))
        elif kind == "union":
            merged = merge_union(left, right)
        elif kind == "concat":
            merged = merge_concat(left, right)
        else:
            raise MergeError(f"step {i}: unknown merge kind {kind!r}")
        sub_results[out_alias] = merged
        last_alias = out_alias

    assert last_alias is not None  # steps was non-empty
    return sub_results[last_alias]


__all__ = [
    "MergeError",
    "merge_join",
    "merge_union",
    "merge_concat",
    "execute_merge_pipeline",
]
