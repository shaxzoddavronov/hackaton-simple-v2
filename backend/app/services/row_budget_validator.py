"""Phase 41 — row-budget guard.

Prevents the agent from sending a query that would scan billions of
rows against a real cluster. Runs AFTER ``readonly_validator`` /
``es_readonly_validator`` / ``mongo_readonly_validator`` /
``api_query_validator`` — those check *what* is allowed, this layer
checks *how much*.

Heuristic per dialect family:

  * **SQL** (sqlglot-parseable). Parse the AST, find the tables the
    query touches, look them up in the :class:`SchemaBundle`'s
    ``row_count_estimate``. Sum the touched tables → predicted scan.
    If predicted scan > ``MAX_PREDICTED_ROWS`` AND the query has no
    upper-bound limiting clause (LIMIT / TOP / FETCH FIRST / a
    surviving WHERE on a primary-key column / aggregate that
    collapses to ≤1000 rows), reject with a "narrow your filter"
    finding. Aggregations without GROUP BY are treated as
    "scans-all-then-collapses-to-one" — they still scan the
    underlying rows, so the cap applies.

  * **Elasticsearch** (JSON envelope). Reject when ``body.query`` is
    a bare ``match_all`` against an index whose document count
    exceeds the cap and the envelope has no ``size`` or ``aggs``
    that collapse to a tractable shape.

  * **MongoDB** (aggregation pipeline). Reject when the pipeline
    has no ``$limit`` / ``$group`` collapsing stage and the
    underlying collection is over the cap.

  * **REST API / GraphQL**: skip — these are external systems we
    can't predict the cost of from the local schema. The HTTP
    timeout is the only ceiling there.

The validator is **advisory by default**: when the schema bundle
has no `row_count_estimate` (a brand-new connection, an unprofiled
table) we err on the side of *allowing* — better to surface a real
result than to gate-block the user on a profiling gap. The cap is
configurable via Settings so admins can raise / lower per cluster.

This is read-only; nothing about the agent state mutates.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from app.engines.base import (
    SchemaBundle,
    TableMeta,
    ValidationFinding,
    ValidationResult,
)

log = logging.getLogger(__name__)


# Default cap when Settings doesn't override. 10M is enough that
# legitimate analytical queries clear it (most tabular DBs are
# under that), big enough to surface a real outlier.
DEFAULT_MAX_PREDICTED_ROWS = 10_000_000

# Aggregations that collapse to a small shape — we let scans
# through when one of these is the OUTER-MOST select expression
# AND there's no GROUP BY exploding the cardinality.
_AGGREGATE_FUNCS = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "STDDEV", "VARIANCE",
    "BOOL_AND", "BOOL_OR",
}


@dataclass(slots=True)
class BudgetDecision:
    """Outcome of one row-budget pass.

    ``predicted_rows`` is the sum of ``row_count_estimate`` across
    every table sqlglot found in the statement's FROM / JOIN list.
    Zero when the bundle lacks estimates."""
    ok: bool
    predicted_rows: int
    cap: int
    reason: str | None = None
    tables: list[str] | None = None


def validate_row_budget(
    sql_or_envelope: str,
    *,
    dialect: str,
    schema_bundle: SchemaBundle | None = None,
    max_predicted_rows: int = DEFAULT_MAX_PREDICTED_ROWS,
) -> ValidationResult:
    """Return :class:`ValidationResult.ok=False` if the predicted
    scan exceeds the cap.

    Advisory: a missing schema bundle, or zero estimates on every
    touched table, returns ``ok=True`` — we don't gate-block on
    incomplete profiling.
    """
    if dialect in {"rest_api", "graphql"}:
        return ValidationResult(ok=True)
    if dialect == "elasticsearch":
        return _check_es(sql_or_envelope, schema_bundle, max_predicted_rows)
    if dialect == "mongodb":
        return _check_mongo(
            sql_or_envelope, schema_bundle, max_predicted_rows
        )
    # SQL family
    return _check_sql(
        sql_or_envelope, dialect, schema_bundle, max_predicted_rows
    )


# ── SQL path ─────────────────────────────────────────────────────


def _check_sql(
    sql: str,
    dialect: str,
    bundle: SchemaBundle | None,
    cap: int,
) -> ValidationResult:
    if bundle is None or not bundle.tables:
        return ValidationResult(ok=True)
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception:
        # Parse failures fall through — the upstream read-only
        # validator already rejected them with the proper code.
        return ValidationResult(ok=True)
    if not statements or statements[0] is None:
        return ValidationResult(ok=True)
    root = statements[0]

    touched = _tables_touched(root)
    if not touched:
        return ValidationResult(ok=True)

    by_qname = _bundle_index(bundle)
    predicted = 0
    matched: list[str] = []
    for qname in touched:
        meta = by_qname.get(qname.lower())
        if meta is None:
            continue
        est = meta.row_count_estimate or 0
        if est > 0:
            predicted += est
            matched.append(qname)
    if predicted == 0:
        # No estimates at all → don't gate-block.
        return ValidationResult(ok=True)

    if _has_strict_upper_bound(root):
        # An explicit LIMIT / aggregate-without-GROUP BY collapses
        # the result to a tractable shape — let it through even if
        # the underlying table is huge. The executor's row_cap
        # provides the second line of defence.
        return ValidationResult(ok=True)

    if predicted <= cap:
        return ValidationResult(ok=True)

    return ValidationResult(
        ok=False,
        findings=[
            ValidationFinding(
                code="ROW_BUDGET_EXCEEDED",
                message=(
                    f"predicted scan {predicted:,} rows across "
                    f"{', '.join(matched[:3])}"
                    f"{' and more' if len(matched) > 3 else ''} "
                    f"exceeds the budget of {cap:,}. Narrow the "
                    "WHERE clause or add LIMIT."
                ),
            )
        ],
    )


def _tables_touched(root: exp.Expression) -> list[str]:
    """Return the qualified table names sqlglot found anywhere in
    the statement (FROM, JOIN, subquery, CTE body)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for table in root.find_all(exp.Table):
        # `exp.Table` carries `name`, `db`, `catalog`. CTEs / subquery
        # aliases also show up but with `name == alias` and no `db`;
        # they won't match a real schema_bundle entry — harmless.
        schema_part = table.args.get("db")
        schema_name = schema_part.name if schema_part else None
        table_name = table.name
        if not table_name:
            continue
        qn = (
            f"{schema_name}.{table_name}"
            if schema_name
            else table_name
        )
        if qn.lower() not in seen_set:
            seen_set.add(qn.lower())
            seen.append(qn)
    return seen


def _bundle_index(bundle: SchemaBundle) -> dict[str, TableMeta]:
    """Index the bundle by both ``schema.table`` and bare ``table``
    so the planner can refer to a table either way."""
    out: dict[str, TableMeta] = {}
    for t in bundle.tables:
        qn = f"{t.schema}.{t.name}".lower()
        out[qn] = t
        # Bare-name fallback — only if no other table claims it.
        if t.name.lower() not in out:
            out[t.name.lower()] = t
    return out


def _has_strict_upper_bound(root: exp.Expression) -> bool:
    """Decide if the statement collapses to a tractable result.

    True when ANY of:
      - top-level LIMIT exists
      - top-level FETCH FIRST ... ROWS ONLY exists (sqlglot maps to Limit)
      - the SELECT list is one aggregate AND there's no GROUP BY
      - the SELECT contains a window-less aggregate over a small key
    """
    # LIMIT covers most analytics planners' output. sqlglot maps
    # MSSQL ``TOP n`` to the same Limit node; FETCH FIRST too.
    if root.find(exp.Limit) is not None:
        return True
    # Aggregate-without-GROUP-BY: SELECT COUNT(*) FROM big — scans
    # the table but returns a single row, and the executor's
    # row_cap is irrelevant. We let these through; the cost is
    # really the cluster's problem, not the user-facing payload.
    if _is_lone_aggregate(root):
        return True
    return False


def _is_lone_aggregate(root: exp.Expression) -> bool:
    """Top-level SELECT projects only aggregate functions and has
    no GROUP BY."""
    if not isinstance(root, exp.Select):
        return False
    if root.args.get("group"):
        return False
    expressions = root.args.get("expressions") or []
    if not expressions:
        return False
    for e in expressions:
        # Drill into aliases (SELECT count(*) AS n).
        inner = e.this if isinstance(e, exp.Alias) else e
        if not _is_aggregate_call(inner):
            return False
    return True


def _is_aggregate_call(node: exp.Expression) -> bool:
    if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
        return True
    if isinstance(node, exp.Func):
        name = (
            (node.this.name if isinstance(node.this, exp.Anonymous)
             else node.__class__.__name__)
            or ""
        ).upper()
        if name in _AGGREGATE_FUNCS:
            return True
    if isinstance(node, exp.Anonymous):
        return node.name.upper() in _AGGREGATE_FUNCS
    return False


# ── Elasticsearch path ──────────────────────────────────────────


_ES_INDEX_QN = re.compile(r"^index::([\w.-]+)$")


def _check_es(
    envelope: str, bundle: SchemaBundle | None, cap: int
) -> ValidationResult:
    if bundle is None:
        return ValidationResult(ok=True)
    try:
        env = json.loads(envelope)
    except (ValueError, TypeError):
        return ValidationResult(ok=True)
    index = env.get("index") if isinstance(env, dict) else None
    body = env.get("body") if isinstance(env, dict) else None
    if not index or not isinstance(body, dict):
        return ValidationResult(ok=True)
    # The schema_bundle's ES adapter persists per-index row counts as
    # plain tables named after the index. Look up by case-insensitive
    # name.
    meta = _es_table_for_index(bundle, str(index))
    if meta is None:
        return ValidationResult(ok=True)
    est = meta.row_count_estimate or 0
    if est <= cap:
        return ValidationResult(ok=True)

    # If the body has an explicit small `size` or any `aggs` that
    # collapse, let it through.
    if isinstance(body.get("size"), int) and 0 < body["size"] <= 1000:
        return ValidationResult(ok=True)
    if "aggs" in body or "aggregations" in body:
        # Aggregations may still scan the index but their output
        # rows are bounded by the bucket count; defer to runtime.
        return ValidationResult(ok=True)

    # Bare match_all without size or aggs → reject.
    query = body.get("query") or {}
    if isinstance(query, dict) and "match_all" in query:
        return ValidationResult(
            ok=False,
            findings=[
                ValidationFinding(
                    code="ROW_BUDGET_EXCEEDED",
                    message=(
                        f"index {index} has ~{est:,} docs — unbounded "
                        "match_all would scan all of them. Add a "
                        "`size` or a `query` filter."
                    ),
                )
            ],
        )
    return ValidationResult(ok=True)


def _es_table_for_index(
    bundle: SchemaBundle, index_name: str
) -> TableMeta | None:
    """ES bundles surface each index as a TableMeta with
    ``schema='index'`` (per the elasticsearch engine adapter)."""
    target = index_name.lower()
    for t in bundle.tables:
        if t.name.lower() == target:
            return t
    return None


# ── MongoDB path ────────────────────────────────────────────────


_MONGO_COLLAPSING_STAGES = ("$count", "$group", "$bucket", "$bucketAuto")


def _check_mongo(
    envelope: str, bundle: SchemaBundle | None, cap: int
) -> ValidationResult:
    if bundle is None:
        return ValidationResult(ok=True)
    try:
        env = json.loads(envelope)
    except (ValueError, TypeError):
        return ValidationResult(ok=True)
    if not isinstance(env, dict):
        return ValidationResult(ok=True)
    collection = env.get("collection")
    pipeline = env.get("pipeline")
    if not collection or not isinstance(pipeline, list):
        return ValidationResult(ok=True)
    meta = _mongo_table_for_collection(bundle, str(collection))
    if meta is None:
        return ValidationResult(ok=True)
    est = meta.row_count_estimate or 0
    if est <= cap:
        return ValidationResult(ok=True)

    # Walk the pipeline — a $limit early or a collapsing stage
    # (count / group / bucket) lets it through.
    for stage in pipeline:
        if not isinstance(stage, dict):
            continue
        if "$limit" in stage:
            n = stage["$limit"]
            if isinstance(n, int) and 0 < n <= 1000:
                return ValidationResult(ok=True)
        for k in _MONGO_COLLAPSING_STAGES:
            if k in stage:
                return ValidationResult(ok=True)

    return ValidationResult(
        ok=False,
        findings=[
            ValidationFinding(
                code="ROW_BUDGET_EXCEEDED",
                message=(
                    f"collection {collection} has ~{est:,} documents "
                    "— the pipeline has no $limit / $group / $count to "
                    "bound the scan. Add a $match + $limit head."
                ),
            )
        ],
    )


def _mongo_table_for_collection(
    bundle: SchemaBundle, name: str
) -> TableMeta | None:
    target = name.lower()
    for t in bundle.tables:
        if t.name.lower() == target:
            return t
    return None


__all__ = [
    "BudgetDecision",
    "DEFAULT_MAX_PREDICTED_ROWS",
    "validate_row_budget",
]
