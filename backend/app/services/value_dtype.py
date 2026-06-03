"""Best-effort dtype inference for engines whose driver doesn't surface
column types (schemaless stores like Mongo / Elasticsearch, dynamically
typed SQLite, REST responses).

The chart_designer rule engine classifies each column as numeric /
time / other from the dtype string. Returning ``"string"`` everywhere
(the historical placeholder) causes every shape to fall to the
``table`` fallback even when a clean bar chart was available. This
module gives schemaless engines a way to derive useful dtype strings
from the first non-null cell of each column.

The vocabulary is intentionally compatible with what the SQL drivers
return: ``int4``, ``int8``, ``numeric``, ``float8``, ``bool``,
``text``, ``timestamptz``, ``date`` — the chart_designer matches on
substrings so any of these works.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Sequence


def infer_value_dtype(value: Any) -> str:
    """Return a dtype string for one cell value, ``""`` if unknown."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, Decimal):
        return "numeric"
    if isinstance(value, float):
        return "float8"
    if isinstance(value, datetime):
        # Naive vs aware doesn't matter for the chart_designer; both
        # match the "timestamp" substring rule.
        return "timestamptz" if value.tzinfo else "timestamp"
    if isinstance(value, date):
        return "date"
    if isinstance(value, time):
        return "time"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (list, tuple, dict)):
        return "jsonb"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "bytea"
    return "text"


def infer_column_dtypes(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    sample: int = 50,
) -> list[str]:
    """Pick a dtype per column by scanning the first ``sample`` rows.

    Stops on the first non-null cell per column (good enough for the
    chart_designer's coarse numeric / time / other split). If every
    sampled cell is None, the dtype falls back to ``"text"`` so the
    column at least sorts left-aligned in the table view.
    """
    n = len(columns)
    out = ["text"] * n
    found = [False] * n
    for row in rows[:sample]:
        if all(found):
            break
        for i in range(min(n, len(row))):
            if found[i]:
                continue
            v = row[i]
            if v is None:
                continue
            t = infer_value_dtype(v)
            if t:
                out[i] = t
                found[i] = True
    return out


__all__ = ["infer_value_dtype", "infer_column_dtypes"]
