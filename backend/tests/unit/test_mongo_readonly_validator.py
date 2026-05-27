"""Spec for the MongoDB read-only validator.

The test corpus IS the spec: every malicious envelope must be rejected
(write stages, arbitrary-code operators, system collections, unknown
stages) and every benign one must be accepted. Mirrors
``test_es_readonly_validator.py`` line-for-line in structure so the
two paradigms stay in lockstep.
"""
from __future__ import annotations

import json

import pytest

from app.services.mongo_readonly_validator import validate_mongo_query


# ── Malicious corpus — every one of these must be rejected ──────────


_REJECT_CASES: list[tuple[str, object]] = [
    (
        "$out stage",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [{"$out": "evil"}],
        },
    ),
    (
        "$merge stage",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [{"$merge": {"into": "evil"}}],
        },
    ),
    (
        "$function in $project",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [
                {
                    "$project": {
                        "x": {
                            "$function": {
                                "body": "function(){}",
                                "args": [],
                                "lang": "js",
                            }
                        }
                    }
                }
            ],
        },
    ),
    (
        "$where in $match",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [{"$match": {"$where": "function(){return true}"}}],
        },
    ),
    (
        "$accumulator deep",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [
                {"$group": {"_id": None, "x": {"$accumulator": {}}}}
            ],
        },
    ),
    (
        "$indexStats system stage",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [{"$indexStats": {}}],
        },
    ),
    (
        "$collStats system stage",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [{"$collStats": {}}],
        },
    ),
    (
        "unknown stage",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [{"$evilOp": {}}],
        },
    ),
    (
        "system database admin",
        {
            "database": "admin",
            "collection": "users",
            "pipeline": [{"$match": {}}],
        },
    ),
    (
        "system database config",
        {
            "database": "config",
            "collection": "settings",
            "pipeline": [{"$match": {}}],
        },
    ),
    (
        "system database local",
        {
            "database": "local",
            "collection": "oplog.rs",
            "pipeline": [{"$match": {}}],
        },
    ),
    (
        "system.users collection",
        {
            "database": "x",
            "collection": "system.users",
            "pipeline": [{"$match": {}}],
        },
    ),
    (
        "system.roles collection",
        {
            "database": "x",
            "collection": "system.roles",
            "pipeline": [{"$match": {}}],
        },
    ),
    (
        "$lookup with $out sub-pipeline",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [
                {
                    "$lookup": {
                        "from": "z",
                        "pipeline": [{"$out": "evil"}],
                        "as": "out",
                    }
                }
            ],
        },
    ),
    (
        "missing database",
        {"collection": "y", "pipeline": []},
    ),
    (
        "missing collection",
        {"database": "x", "pipeline": []},
    ),
    (
        "non-list pipeline",
        {"database": "x", "collection": "y", "pipeline": "not a list"},
    ),
    (
        "multi-key stage object",
        {
            "database": "x",
            "collection": "y",
            "pipeline": [{"$match": {}, "$sort": {}}],
        },
    ),
    (
        "non-object envelope",
        ["not", "a", "dict"],
    ),
]


@pytest.mark.parametrize(
    "name,envelope",
    _REJECT_CASES,
    ids=[c[0] for c in _REJECT_CASES],
)
def test_rejects(name: str, envelope: object) -> None:
    """Every malicious shape must be rejected with at least one finding."""
    result, _ = validate_mongo_query(json.dumps(envelope))
    assert not result.ok, f"{name!r} should have been rejected"
    assert result.findings, f"{name!r} rejected with no findings"


def test_rejects_invalid_json() -> None:
    result, _ = validate_mongo_query("not json")
    assert not result.ok
    assert result.findings[0].code == "PARSE_ERROR"


# ── Benign corpus — must accept ─────────────────────────────────────


def test_accepts_match_group_sort_limit() -> None:
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [
            {"$match": {"status": "paid"}},
            {"$group": {"_id": "$customer_id", "total": {"$sum": "$amount"}}},
            {"$sort": {"total": -1}},
            {"$limit": 10},
        ],
    }
    result, parsed = validate_mongo_query(json.dumps(env))
    assert result.ok, result.findings
    # Explicit $limit must be preserved, not duplicated.
    assert sum(1 for s in parsed["pipeline"] if "$limit" in s) == 1
    assert parsed["pipeline"][-1] == {"$limit": 10}


def test_accepts_facet() -> None:
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [
            {
                "$facet": {
                    "by_region": [
                        {"$group": {"_id": "$region", "n": {"$sum": 1}}}
                    ],
                    "by_status": [
                        {"$group": {"_id": "$status", "n": {"$sum": 1}}}
                    ],
                }
            }
        ],
    }
    result, _ = validate_mongo_query(json.dumps(env))
    assert result.ok, result.findings


def test_accepts_lookup_without_write_subpipeline() -> None:
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [
            {
                "$lookup": {
                    "from": "customers",
                    "localField": "customer_id",
                    "foreignField": "_id",
                    "as": "customer",
                }
            },
            {"$limit": 50},
        ],
    }
    result, _ = validate_mongo_query(json.dumps(env))
    assert result.ok, result.findings


def test_accepts_lookup_with_safe_subpipeline() -> None:
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [
            {
                "$lookup": {
                    "from": "customers",
                    "pipeline": [
                        {"$match": {"active": True}},
                        {"$project": {"name": 1, "email": 1}},
                    ],
                    "as": "customer",
                }
            }
        ],
    }
    result, _ = validate_mongo_query(json.dumps(env))
    assert result.ok, result.findings


def test_injects_default_limit_when_missing() -> None:
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [{"$match": {"status": "paid"}}],
    }
    result, parsed = validate_mongo_query(json.dumps(env))
    assert result.ok, result.findings
    # Default $limit appended.
    assert parsed["pipeline"][-1] == {"$limit": 1000}


def test_accepts_envelope_as_dict() -> None:
    """Validator should accept an already-parsed dict, not just a string."""
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [{"$match": {}}],
    }
    result, parsed = validate_mongo_query(env)
    assert result.ok, result.findings
    # Default limit should still be injected.
    assert parsed["pipeline"][-1] == {"$limit": 1000}


def test_accepts_count_stage() -> None:
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [
            {"$match": {"status": "paid"}},
            {"$count": "paid_orders"},
        ],
    }
    result, _ = validate_mongo_query(json.dumps(env))
    assert result.ok, result.findings


def test_accepts_bucket_and_unwind() -> None:
    env = {
        "database": "shop",
        "collection": "orders",
        "pipeline": [
            {"$unwind": "$items"},
            {
                "$bucket": {
                    "groupBy": "$items.price",
                    "boundaries": [0, 10, 100, 1000],
                    "default": "other",
                }
            },
        ],
    }
    result, _ = validate_mongo_query(json.dumps(env))
    assert result.ok, result.findings
