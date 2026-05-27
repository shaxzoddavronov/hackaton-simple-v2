"""Pick a UISpec variant from the executed result.

Deterministic rules drive the choice — we tried letting the LLM pick the
variant via guided JSON-schema decoding, but it kept defaulting to bar
charts even for single-row results. Rules are cheaper, faster, and the
mapping from result-shape to chart-family is genuinely mechanical:

  - 0 rows                              → text_only "No rows returned."
  - 1 row × 1 numeric col                → kpi
  - 1 row × many cols                    → table (vertical record)
  - >1 row, 1 time/date col + numerics   → line
  - >1 row, 1 category col + 1 numeric   → bar
  - 2 columns, label + share of total    → pie  (top-N <= 8)
  - everything else                      → table

The LLM was originally meant to pick this; with strict ``response_format``
half-supported by our local vLLM and a degraded output failure mode
(thousands of trailing newlines, see ``app.agents.llm``), the simpler
deterministic path is the right call.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.agents.state import GraphState
from app.engines.base import ResultSet
from app.schemas.ui_spec import (
    BarSpec,
    ColumnDef,
    KPI,
    LineSpec,
    PieSpec,
    TableSpec,
    TextOnly,
    UISpec,
)

log = logging.getLogger(__name__)


_NUMERIC_TYPES = {
    "int",
    "int4",
    "int8",
    "integer",
    "bigint",
    "smallint",
    "numeric",
    "decimal",
    "float",
    "float4",
    "float8",
    "real",
    "double precision",
    "money",
}


_TIME_TYPES = {
    "date",
    "time",
    "timestamp",
    "timestamptz",
    "timestamp without time zone",
    "timestamp with time zone",
    "datetime",
}


_TIME_COLUMN_NAME = re.compile(r"^(ts|at|date|day|month|year|week|.*_at)$", re.I)


def _is_numeric(dtype: str) -> bool:
    dt = (dtype or "").lower()
    return any(t in dt for t in _NUMERIC_TYPES)


def _is_time(dtype: str, name: str) -> bool:
    dt = (dtype or "").lower()
    if any(t in dt for t in _TIME_TYPES):
        return True
    return bool(_TIME_COLUMN_NAME.match(name or ""))


def _column_indices(rs: ResultSet) -> tuple[list[int], list[int], list[int]]:
    """Return indices of (time, numeric, other) columns."""
    times: list[int] = []
    nums: list[int] = []
    others: list[int] = []
    for i, (name, dtype) in enumerate(zip(rs.columns, rs.dtypes)):
        if _is_time(dtype, name):
            times.append(i)
        elif _is_numeric(dtype):
            nums.append(i)
        else:
            others.append(i)
    return times, nums, others


def _rows_as_dicts(rs: ResultSet) -> list[dict[str, Any]]:
    return [dict(zip(rs.columns, row)) for row in rs.rows]


def _pretty_label(col: str) -> str:
    return col.replace("_", " ").strip().title()


def _table_spec(rs: ResultSet) -> TableSpec:
    columns = [
        ColumnDef(
            key=name,
            label=_pretty_label(name),
            dtype=_table_dtype(dtype),
            align="right" if _is_numeric(dtype) else "left",
        )
        for name, dtype in zip(rs.columns, rs.dtypes)
    ]
    return TableSpec(type="table", columns=columns, rows=rs.rows)


def _table_dtype(dtype: str) -> str:
    dt = (dtype or "").lower()
    if any(t in dt for t in ("int", "bigint", "smallint")):
        return "int"
    if any(t in dt for t in ("numeric", "decimal", "float", "real", "double")):
        return "float"
    if any(t in dt for t in ("bool",)):
        return "bool"
    if any(t in dt for t in ("timestamp", "datetime")):
        return "datetime"
    if "date" in dt:
        return "date"
    return "string"


def _pick_spec(rs: ResultSet, user_message: str) -> UISpec:
    if rs.row_count == 0:
        return TextOnly(type="text_only", body_md="No rows returned.")

    times, nums, others = _column_indices(rs)

    # ── Single row → KPI for first numeric, else compact table ────────
    if rs.row_count == 1:
        row = rs.rows[0]
        if nums:
            num_idx = nums[0]
            raw = row[num_idx]
            # KPI.value is ``float | str`` per the Pydantic schema, but
            # a misbehaving driver (asyncpg returning a JSONB cell, a
            # Postgres composite type, an ES aggregation we didn't
            # flatten cleanly) could hand us a dict here. Pydantic
            # would coerce silently and the frontend would then crash
            # with "Objects are not valid as a React child". Force a
            # primitive shape ourselves so the contract holds.
            value = _coerce_to_primitive(raw)
            label_idx = others[0] if others else None
            if label_idx is not None:
                label_val = _coerce_to_primitive(row[label_idx])
                label = f"{_pretty_label(rs.columns[num_idx])} — {label_val}"
            else:
                label = _pretty_label(rs.columns[num_idx])
            return KPI(
                type="kpi",
                label=label,
                value=value if value is not None else 0,
            )
        # Single row, all non-numeric — show as a tiny table.
        return _table_spec(rs)

    # ── Time series → line ────────────────────────────────────────────
    if times and nums and len(nums) >= 1 and len(others) == 0:
        x = rs.columns[times[0]]
        ys = [rs.columns[i] for i in nums]
        return LineSpec(
            type="line",
            title=_pretty_label(x) + " trend",
            x=x,
            y=ys,
            data=_rows_as_dicts(rs),
        )

    # ── 1 category + 1 numeric → bar (or pie for small N) ─────────────
    if len(others) == 1 and len(nums) == 1 and not times:
        x = rs.columns[others[0]]
        y = rs.columns[nums[0]]
        rows = _rows_as_dicts(rs)
        # Pie only when the result reads naturally as a part-of-whole
        # share: 2–8 rows, mention-of "share/percent/distribution".
        if 2 <= rs.row_count <= 8 and re.search(
            r"share|percent|distribution|ulush|tarqalish",
            user_message or "",
            re.I,
        ):
            return PieSpec(
                type="pie",
                title=_pretty_label(y),
                label=x,
                value=y,
                data=rows,
            )
        return BarSpec(
            type="bar",
            title=_pretty_label(y) + " by " + _pretty_label(x),
            x=x,
            y=[y],
            data=rows,
        )

    # ── Many columns or unusual shape → table ─────────────────────────
    return _table_spec(rs)


async def run(state: GraphState) -> GraphState:
    rs = state.get("result")
    if rs is None:
        return {"chart": None}
    try:
        spec = _pick_spec(rs, state.get("user_message", ""))
    except Exception:
        log.exception("chart_designer: rule pick failed; falling back to text")
        spec = TextOnly(type="text_only", body_md="(could not build a chart)")
    return {"chart": spec}


def _coerce_to_primitive(value: Any) -> Any:
    """Return a JSON-renderable primitive for ``value``.

    Postgres / ES drivers return rich types we can't put straight into
    a KPI label or a chart datapoint: Decimal, datetime, asyncpg
    composite tuples, JSONB cells holding a dict, and so on. Pydantic
    will silently accept some of these and then the frontend explodes
    with "Objects are not valid as a React child". We pre-flatten:

      * ``None`` → ``None`` (caller decides how to display).
      * bool/int/float/str → unchanged.
      * datetime / date / time → ISO string.
      * Decimal → float (lossless for analytics-scale numbers).
      * dict / list / tuple → JSON-encoded string.
      * Anything else → ``str(...)``.
    """
    from datetime import date, datetime, time
    from decimal import Decimal
    import json as _json

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Decimal):
        try:
            return float(value)
        except (ValueError, OverflowError):
            return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        try:
            return _json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)
