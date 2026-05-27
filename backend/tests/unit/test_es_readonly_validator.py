"""Spec for the Elasticsearch read-only validator.

The test corpus IS the spec: every malicious shape below MUST be
rejected, every benign one MUST be accepted. Same contract as
``test_readonly_validator.py`` for SQL.
"""
from __future__ import annotations

import json

import pytest

from app.services.es_readonly_validator import validate_es_query


# ── Malicious corpus — every one of these must be rejected ──────────


_REJECT_CASES: list[tuple[str, dict]] = [
    (
        "script in query.script_score",
        {
            "index": "orders",
            "body": {
                "query": {
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {"source": "doc['x'].value * 2"},
                    }
                }
            },
        },
    ),
    (
        "script_fields at top level",
        {
            "index": "orders",
            "body": {
                "query": {"match_all": {}},
                "script_fields": {
                    "doubled": {"script": {"source": "doc['x'].value * 2"}}
                },
            },
        },
    ),
    (
        "scripted_metric agg",
        {
            "index": "orders",
            "body": {
                "size": 0,
                "aggs": {
                    "bad": {
                        "scripted_metric": {
                            "init_script": "state.x = 0",
                            "map_script": "state.x += 1",
                            "combine_script": "return state.x",
                            "reduce_script": "long s = 0; for (a in states) s += a; return s",
                        }
                    }
                },
            },
        },
    ),
    (
        "runtime_mappings.script",
        {
            "index": "orders",
            "body": {
                "runtime_mappings": {
                    "danger": {
                        "type": "long",
                        "script": "emit(1)",
                    }
                },
                "query": {"match_all": {}},
            },
        },
    ),
    (
        "deep nested script",
        {
            "index": "orders",
            "body": {
                "query": {
                    "bool": {
                        "filter": [
                            {"script": {"script": "doc['x'].value > 0"}}
                        ]
                    }
                }
            },
        },
    ),
    (
        "_delete_by_query envelope key",
        {
            "index": "orders",
            "body": {
                "_delete_by_query": {"query": {"match_all": {}}}
            },
        },
    ),
    (
        "_update_by_query envelope key",
        {"index": "orders", "body": {"_update_by_query": {}}},
    ),
    (
        "_reindex envelope key",
        {"index": "orders", "body": {"_reindex": {}}},
    ),
    (
        "system index .security",
        {"index": ".security", "body": {"query": {"match_all": {}}}},
    ),
    (
        "dot-prefixed index",
        {"index": ".watches", "body": {"query": {"match_all": {}}}},
    ),
    (
        "wildcard touching .kibana",
        {"index": ".kibana*", "body": {"query": {"match_all": {}}}},
    ),
    (
        "top-level unknown key",
        {
            "index": "orders",
            "body": {"query": {"match_all": {}}, "evil_extension": {}},
        },
    ),
    (
        "missing index",
        {"body": {"query": {"match_all": {}}}},
    ),
    (
        "missing body",
        {"index": "orders"},
    ),
    (
        "non-object body",
        {"index": "orders", "body": "select * from x"},
    ),
]


@pytest.mark.parametrize("name,envelope", _REJECT_CASES, ids=[c[0] for c in _REJECT_CASES])
def test_rejects(name: str, envelope: dict) -> None:
    result, _ = validate_es_query(json.dumps(envelope))
    assert not result.ok, f"{name!r} should have been rejected"
    assert result.findings, f"{name!r} rejected with no findings"


def test_rejects_invalid_json() -> None:
    result, _ = validate_es_query("not a json {")
    assert not result.ok
    assert result.findings[0].code == "PARSE_ERROR"


# ── Benign corpus — must accept ─────────────────────────────────────


def test_accepts_plain_search() -> None:
    env = {
        "index": "orders",
        "body": {"query": {"match_all": {}}, "size": 10},
    }
    result, parsed = validate_es_query(json.dumps(env))
    assert result.ok, result.findings
    assert parsed["body"]["size"] == 10
    assert parsed["body"]["timeout"] == "10s"  # default injected


def test_accepts_terms_aggregation() -> None:
    env = {
        "index": "orders",
        "body": {
            "size": 0,
            "query": {"bool": {"filter": [{"range": {"ts": {"gte": "now-30d"}}}]}},
            "aggs": {
                "by_region": {
                    "terms": {"field": "region.keyword", "size": 5},
                    "aggs": {"revenue": {"sum": {"field": "amount"}}},
                }
            },
        },
    }
    result, _ = validate_es_query(json.dumps(env))
    assert result.ok, result.findings


def test_accepts_date_histogram() -> None:
    env = {
        "index": "events-*",
        "body": {
            "size": 0,
            "aggs": {
                "trend": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "calendar_interval": "day",
                    }
                }
            },
        },
    }
    result, _ = validate_es_query(json.dumps(env))
    assert result.ok, result.findings


def test_caps_oversized_size() -> None:
    env = {
        "index": "orders",
        "body": {"query": {"match_all": {}}, "size": 10_000_000},
    }
    result, parsed = validate_es_query(json.dumps(env))
    assert result.ok
    assert parsed["body"]["size"] == 1000


def test_aggs_default_size_to_zero() -> None:
    env = {
        "index": "orders",
        "body": {"aggs": {"c": {"value_count": {"field": "id"}}}},
    }
    result, parsed = validate_es_query(json.dumps(env))
    assert result.ok
    assert parsed["body"]["size"] == 0


def test_accepts_envelope_as_dict() -> None:
    # Validator should accept already-parsed envelope, not require JSON str.
    env = {"index": "x", "body": {"query": {"match_all": {}}}}
    result, _ = validate_es_query(env)
    assert result.ok
