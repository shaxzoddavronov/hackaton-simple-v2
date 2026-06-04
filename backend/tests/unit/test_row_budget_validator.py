"""Phase 41 — row-budget guard validator tests."""
from __future__ import annotations

import json

import pytest

from app.engines.base import (
    ColumnMeta,
    SchemaBundle,
    TableMeta,
)
from app.services.row_budget_validator import (
    DEFAULT_MAX_PREDICTED_ROWS,
    validate_row_budget,
)


# ── helpers ──────────────────────────────────────────────────────


def _t(
    schema: str,
    name: str,
    *,
    rows: int | None,
    columns: list[ColumnMeta] | None = None,
) -> TableMeta:
    return TableMeta(
        schema=schema,
        name=name,
        columns=columns or [],
        row_count_estimate=rows,
    )


def _bundle(*tables: TableMeta, dialect: str = "postgres") -> SchemaBundle:
    return SchemaBundle(dialect=dialect, tables=list(tables))


# ── No-schema / no-estimate paths (advisory pass-through) ──────


def test_no_bundle_passes() -> None:
    r = validate_row_budget(
        "SELECT * FROM orders",
        dialect="postgres",
        schema_bundle=None,
    )
    assert r.ok


def test_empty_bundle_passes() -> None:
    r = validate_row_budget(
        "SELECT * FROM orders",
        dialect="postgres",
        schema_bundle=_bundle(),
    )
    assert r.ok


def test_no_estimate_on_touched_table_passes() -> None:
    """A table without a row_count_estimate (None or 0) must NOT
    gate-block — we don't punish a fresh / unprofiled connection."""
    r = validate_row_budget(
        "SELECT * FROM orders",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "orders", rows=None)),
    )
    assert r.ok


def test_unparseable_sql_passes_through() -> None:
    """The upstream read-only validator rejects parse errors with
    a dedicated finding; the row-budget layer shouldn't double-up.
    A garbled query gets ``ok=True`` here so the chain decides
    elsewhere."""
    r = validate_row_budget(
        "SELEC garbled !!!",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "orders", rows=20_000_000)),
    )
    assert r.ok


# ── SQL — over-cap, no LIMIT, no aggregate → reject ────────────


def test_huge_scan_without_limit_is_rejected() -> None:
    r = validate_row_budget(
        "SELECT * FROM events",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert not r.ok
    assert r.findings[0].code == "ROW_BUDGET_EXCEEDED"
    msg = r.findings[0].message
    assert "events" in msg
    assert "50,000,000" in msg


def test_join_over_two_huge_tables_is_rejected() -> None:
    r = validate_row_budget(
        "SELECT * FROM orders o JOIN customers c ON o.cid = c.id",
        dialect="postgres",
        schema_bundle=_bundle(
            _t("public", "orders", rows=30_000_000),
            _t("public", "customers", rows=20_000_000),
        ),
        max_predicted_rows=10_000_000,
    )
    assert not r.ok
    # Both tables' estimates sum together → over the cap.


# ── SQL — under-cap → pass ─────────────────────────────────────


def test_small_table_passes() -> None:
    r = validate_row_budget(
        "SELECT * FROM users",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "users", rows=500)),
    )
    assert r.ok


def test_cap_just_above_estimate_passes() -> None:
    r = validate_row_budget(
        "SELECT * FROM orders",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "orders", rows=9_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert r.ok


# ── SQL — escape hatches ───────────────────────────────────────


def test_explicit_limit_allows_huge_table() -> None:
    r = validate_row_budget(
        "SELECT * FROM events LIMIT 100",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_count_aggregate_without_group_by_passes() -> None:
    """COUNT(*) returns a single row even if the underlying table is
    huge. The cluster pays the scan cost but we don't risk a
    runaway result payload — let it through."""
    r = validate_row_budget(
        "SELECT COUNT(*) FROM events",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_sum_with_alias_passes() -> None:
    r = validate_row_budget(
        "SELECT SUM(amount) AS total FROM events",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_aggregate_with_group_by_is_NOT_a_pass() -> None:
    """SELECT user_id, COUNT(*) FROM huge GROUP BY user_id can still
    explode the row count — group-by is NOT a collapsing
    escape-hatch."""
    r = validate_row_budget(
        "SELECT user_id, COUNT(*) FROM events GROUP BY user_id",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert not r.ok


# ── ES + Mongo + rest_api + graphql ─────────────────────────────


def test_es_unbounded_match_all_against_huge_index_rejects() -> None:
    envelope = json.dumps(
        {
            "index": "events",
            "body": {"query": {"match_all": {}}},
        }
    )
    bundle = SchemaBundle(
        dialect="elasticsearch",
        tables=[_t("index", "events", rows=80_000_000)],
    )
    r = validate_row_budget(
        envelope,
        dialect="elasticsearch",
        schema_bundle=bundle,
        max_predicted_rows=10_000_000,
    )
    assert not r.ok
    assert "events" in r.findings[0].message


def test_es_match_all_with_size_passes() -> None:
    envelope = json.dumps(
        {
            "index": "events",
            "body": {"query": {"match_all": {}}, "size": 100},
        }
    )
    bundle = SchemaBundle(
        dialect="elasticsearch",
        tables=[_t("index", "events", rows=80_000_000)],
    )
    r = validate_row_budget(
        envelope,
        dialect="elasticsearch",
        schema_bundle=bundle,
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_es_aggs_passes() -> None:
    """Aggregation buckets bound the response shape — defer to
    runtime."""
    envelope = json.dumps(
        {
            "index": "events",
            "body": {
                "query": {"match_all": {}},
                "aggs": {"by_kind": {"terms": {"field": "kind"}}},
            },
        }
    )
    bundle = SchemaBundle(
        dialect="elasticsearch",
        tables=[_t("index", "events", rows=80_000_000)],
    )
    r = validate_row_budget(
        envelope,
        dialect="elasticsearch",
        schema_bundle=bundle,
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_es_with_filter_query_passes() -> None:
    """A bool/term/range filter narrows scan — pass through, the
    cluster decides whether to honour."""
    envelope = json.dumps(
        {
            "index": "events",
            "body": {
                "query": {"term": {"kind": "click"}},
            },
        }
    )
    bundle = SchemaBundle(
        dialect="elasticsearch",
        tables=[_t("index", "events", rows=80_000_000)],
    )
    r = validate_row_budget(
        envelope,
        dialect="elasticsearch",
        schema_bundle=bundle,
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_mongo_pipeline_without_limit_or_group_rejects() -> None:
    envelope = json.dumps(
        {
            "database": "app",
            "collection": "events",
            "pipeline": [{"$match": {"kind": "click"}}],
        }
    )
    bundle = SchemaBundle(
        dialect="mongodb",
        tables=[_t("app", "events", rows=80_000_000)],
    )
    r = validate_row_budget(
        envelope,
        dialect="mongodb",
        schema_bundle=bundle,
        max_predicted_rows=10_000_000,
    )
    assert not r.ok


def test_mongo_pipeline_with_limit_passes() -> None:
    envelope = json.dumps(
        {
            "database": "app",
            "collection": "events",
            "pipeline": [
                {"$match": {"kind": "click"}},
                {"$limit": 50},
            ],
        }
    )
    bundle = SchemaBundle(
        dialect="mongodb",
        tables=[_t("app", "events", rows=80_000_000)],
    )
    r = validate_row_budget(
        envelope,
        dialect="mongodb",
        schema_bundle=bundle,
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_mongo_pipeline_with_group_passes() -> None:
    envelope = json.dumps(
        {
            "database": "app",
            "collection": "events",
            "pipeline": [{"$group": {"_id": "$kind", "n": {"$sum": 1}}}],
        }
    )
    bundle = SchemaBundle(
        dialect="mongodb",
        tables=[_t("app", "events", rows=80_000_000)],
    )
    r = validate_row_budget(
        envelope,
        dialect="mongodb",
        schema_bundle=bundle,
        max_predicted_rows=10_000_000,
    )
    assert r.ok


def test_rest_api_dialect_always_passes() -> None:
    """External APIs aren't budgeted by the local schema — the
    HTTP timeout is the only ceiling. Skip the check."""
    r = validate_row_budget(
        '{"endpoint":"/x","method":"GET"}',
        dialect="rest_api",
        schema_bundle=_bundle(_t("public", "anything", rows=10**9)),
    )
    assert r.ok


def test_graphql_dialect_always_passes() -> None:
    r = validate_row_budget(
        '{"query":"{ x }"}',
        dialect="graphql",
        schema_bundle=_bundle(_t("public", "anything", rows=10**9)),
    )
    assert r.ok


# ── Default cap sane ───────────────────────────────────────────


def test_default_max_predicted_rows_is_reasonable() -> None:
    """Guard against drift — too low and every analytics query
    trips; too high and the cap is useless."""
    assert 1_000_000 <= DEFAULT_MAX_PREDICTED_ROWS <= 1_000_000_000


# ── Tables-touched is case-insensitive, handles aliases ─────────


def test_aliased_table_still_matches_bundle() -> None:
    r = validate_row_budget(
        "SELECT * FROM events AS e",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert not r.ok


def test_schema_qualified_lookup_matches() -> None:
    r = validate_row_budget(
        "SELECT * FROM public.events",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    assert not r.ok


def test_cte_alias_does_not_count_as_real_table() -> None:
    """A `WITH X AS (SELECT ...)` introduces a name that doesn't
    exist in the schema bundle. The budget layer should look up
    only real tables and not double-count the CTE alias."""
    r = validate_row_budget(
        "WITH recent AS (SELECT * FROM events LIMIT 1000) "
        "SELECT COUNT(*) FROM recent",
        dialect="postgres",
        schema_bundle=_bundle(_t("public", "events", rows=50_000_000)),
        max_predicted_rows=10_000_000,
    )
    # Outer is COUNT(*) — lone aggregate → pass even though events
    # is huge. The CTE has its own LIMIT inside; either way the
    # validator should accept this shape.
    assert r.ok
