"""Smoke tests for the ElasticsearchEngine adapter.

The real ``AsyncElasticsearch`` client is monkey-patched with a fake
that returns canned ``Response``-shaped dicts. We assert:

  * introspect_schema flattens mappings into our ``SchemaBundle``.
  * execute() routes aggs through ``_flatten_aggregations`` and
    returns a tabular ``ResultSet``.
  * validate_readonly delegates to the JSON-DSL validator.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.engines.elasticsearch import (
    ElasticsearchEngine,
    _flatten_aggregations,
    _flatten_properties,
)


class _Resp:
    """Minimal stand-in for elasticsearch's ApiResponse — it just
    exposes a ``.body`` attribute matching what the engine reads."""

    def __init__(self, payload: dict) -> None:
        self.body = payload


class _FakeIndices:
    def __init__(self, mappings: dict) -> None:
        self._mappings = mappings

    async def get_mapping(self, *, index: str, expand_wildcards: str) -> _Resp:
        return _Resp(self._mappings)


class _FakeClient:
    def __init__(
        self,
        *,
        mappings: dict | None = None,
        search_response: dict | None = None,
    ) -> None:
        self.indices = _FakeIndices(mappings or {})
        self._search_response = search_response or {"hits": {"hits": []}}
        self.last_search: tuple[str, dict] | None = None

    async def search(self, *, index: str, body: dict, **_kw) -> _Resp:
        self.last_search = (index, body)
        return _Resp(self._search_response)

    async def close(self) -> None:
        return None


def _make_engine(fake: _FakeClient) -> ElasticsearchEngine:
    """Build an engine and swap its client for our fake."""
    source = SimpleNamespace(
        connection_meta={"hosts": ["http://localhost:9200"]},
        _credentials={},
    )
    engine = ElasticsearchEngine(source)
    engine._client = fake  # type: ignore[attr-defined]
    return engine


# ── introspect_schema ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_introspect_schema_flattens_mappings() -> None:
    fake = _FakeClient(
        mappings={
            "orders": {
                "mappings": {
                    "properties": {
                        "amount": {"type": "double"},
                        "region": {"type": "keyword"},
                        "ts": {"type": "date"},
                        "customer": {
                            "properties": {
                                "id": {"type": "long"},
                                "email": {"type": "keyword"},
                            }
                        },
                    }
                }
            },
            # Hidden index — must be skipped.
            ".security": {"mappings": {"properties": {"x": {"type": "long"}}}},
        }
    )
    eng = _make_engine(fake)
    bundle = await eng.introspect_schema()
    table_names = {t.name for t in bundle.tables}
    assert table_names == {"orders"}  # hidden one excluded
    orders = next(t for t in bundle.tables if t.name == "orders")
    col_names = {c.name for c in orders.columns}
    assert {"amount", "region", "ts", "customer", "customer.id", "customer.email"} <= col_names
    # ES dates become our generic 'timestamp' dtype
    ts = next(c for c in orders.columns if c.name == "ts")
    assert ts.data_type == "timestamp"


# ── execute → ResultSet ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_aggs_returns_table() -> None:
    fake = _FakeClient(
        search_response={
            "hits": {"hits": []},
            "aggregations": {
                "by_region": {
                    "buckets": [
                        {
                            "key": "EMEA",
                            "doc_count": 12,
                            "revenue": {"value": 1500.5},
                        },
                        {
                            "key": "APAC",
                            "doc_count": 7,
                            "revenue": {"value": 900.0},
                        },
                    ]
                }
            },
        }
    )
    eng = _make_engine(fake)
    envelope = {
        "index": "orders",
        "body": {
            "size": 0,
            "aggs": {
                "by_region": {
                    "terms": {"field": "region.keyword", "size": 5},
                    "aggs": {"revenue": {"sum": {"field": "amount"}}},
                }
            },
        },
    }
    rs = await eng.execute(json.dumps(envelope))
    assert rs.columns == ["by_region", "doc_count", "revenue"]
    assert rs.rows == [
        ["EMEA", 12, 1500.5],
        ["APAC", 7, 900.0],
    ]
    assert rs.row_count == 2


@pytest.mark.asyncio
async def test_execute_hits_returns_doc_rows() -> None:
    fake = _FakeClient(
        search_response={
            "hits": {
                "hits": [
                    {"_source": {"id": 1, "user": "ali"}},
                    {"_source": {"id": 2, "user": "bobur", "extra": "x"}},
                ]
            }
        }
    )
    eng = _make_engine(fake)
    envelope = {"index": "users", "body": {"query": {"match_all": {}}, "size": 50}}
    rs = await eng.execute(json.dumps(envelope))
    # Columns are union of all _source keys, in first-seen order.
    assert rs.columns == ["id", "user", "extra"]
    assert rs.rows == [[1, "ali", None], [2, "bobur", "x"]]


@pytest.mark.asyncio
async def test_execute_refuses_invalid_envelope() -> None:
    eng = _make_engine(_FakeClient())
    bad = json.dumps({"index": ".security", "body": {"query": {"match_all": {}}}})
    with pytest.raises(ValueError):
        await eng.execute(bad)


# ── validate_readonly direct ────────────────────────────────────────


def test_validate_readonly_passthrough() -> None:
    eng = _make_engine(_FakeClient())
    good = json.dumps({"index": "orders", "body": {"query": {"match_all": {}}}})
    assert eng.validate_readonly(good).ok

    bad = json.dumps({"index": "orders", "body": {"script_fields": {"x": {"script": {"source": "1"}}}}})
    assert not eng.validate_readonly(bad).ok


# ── Pure helpers ───────────────────────────────────────────────────


def test_flatten_properties_handles_nested_objects() -> None:
    props = {
        "name": {"type": "text"},
        "address": {
            "properties": {
                "city": {"type": "keyword"},
                "zip": {"type": "keyword"},
            }
        },
    }
    cols = _flatten_properties(props)
    names = [c.name for c in cols]
    assert "address" in names
    assert "address.city" in names
    assert "address.zip" in names


def test_flatten_aggregations_date_histogram_marks_timestamp() -> None:
    """date_histogram buckets carry ISO ``key_as_string`` values. The
    chart_designer needs the key column dtype to be ``timestamp`` so
    it picks LineChart over a fallback table for trend questions."""
    aggs = {
        "revenue_trend": {
            "buckets": [
                {
                    "key": 1704067200000,
                    "key_as_string": "2024-01-01T00:00:00.000Z",
                    "doc_count": 463,
                    "total_revenue": {"value": 577568689.88},
                },
                {
                    "key": 1706745600000,
                    "key_as_string": "2024-02-01T00:00:00.000Z",
                    "doc_count": 505,
                    "total_revenue": {"value": 758031452.0},
                },
            ]
        }
    }
    cols, dtypes, rows = _flatten_aggregations(aggs)
    assert cols == ["revenue_trend", "doc_count", "total_revenue"]
    # Critical: the key dtype is timestamp, not string. Without this
    # chart_designer falls back to a TableSpec for trends.
    assert dtypes[0] == "timestamp"
    assert dtypes[1] == "bigint"
    assert rows[0][0] == "2024-01-01T00:00:00.000Z"
    assert rows[0][2] == 577568689.88


def test_flatten_aggregations_terms_keeps_string() -> None:
    """Non-date bucket keys (terms agg on a keyword field) stay as
    'string' so they don't accidentally trigger time-series rendering."""
    aggs = {
        "by_region": {
            "buckets": [
                {"key": "EMEA", "doc_count": 12},
                {"key": "APAC", "doc_count": 7},
            ]
        }
    }
    _cols, dtypes, _rows = _flatten_aggregations(aggs)
    assert dtypes[0] == "string"


def test_flatten_aggregations_unwraps_doc_count_sub_agg() -> None:
    """Regression: when the planner writes ``aggs.doc_count.value_count``
    by accident, every bucket's ``doc_count`` field is ``{"value": N}``
    instead of an integer. ``_flatten_aggregations`` used to forward the
    dict into the row, which leaked into BarSpec.data as ``{value: 0}``
    and crashed the frontend. The unwrap now happens at the source."""
    aggs = {
        "by_department": {
            "buckets": [
                {"key": "support", "doc_count": {"value": 0}},
                {"key": "sales", "doc_count": {"value": 7}},
            ]
        }
    }
    cols, _dtypes, rows = _flatten_aggregations(aggs)
    assert cols[:2] == ["by_department", "doc_count"]
    # Cells are now scalars, not dicts.
    assert rows[0][1] == 0
    assert rows[1][1] == 7


def test_flatten_aggregations_metric_only() -> None:
    # A top-level metric (e.g., value_count, sum) without buckets.
    cols, _dtypes, rows = _flatten_aggregations(
        {"total": {"value": 42}}
    )
    assert cols == ["total"]
    assert rows == [[42]]
