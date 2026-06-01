"""Phase 30 — BigQuery engine adapter."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.engines.bigquery import (
    BigQueryEngine,
    _bq_dtype,
    _coerce_value,
)


# ── dtype mapping ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field_type,expected",
    [
        ("INT64", "bigint"),
        ("INTEGER", "bigint"),
        ("BIGINT", "bigint"),
        ("FLOAT64", "double"),
        ("FLOAT", "double"),
        ("REAL", "double"),
        ("DOUBLE", "double"),
        ("NUMERIC", "decimal"),
        ("BIGNUMERIC", "decimal"),
        ("DECIMAL", "decimal"),
        ("STRING", "text"),
        ("BYTES", "text"),
        ("BOOL", "boolean"),
        ("BOOLEAN", "boolean"),
        ("DATE", "date"),
        ("TIMESTAMP", "timestamp"),
        ("DATETIME", "timestamp"),
        ("TIME", "timestamp"),
        ("JSON", "json"),
        ("GEOGRAPHY", "text"),
        ("ARRAY<INT64>", "array"),
        ("STRUCT<a INT64>", "object"),
        ("UNKNOWN_TYPE_2030", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_bq_dtype_mapping(field_type, expected) -> None:
    assert _bq_dtype(field_type) == expected


def test_bq_dtype_case_insensitive() -> None:
    assert _bq_dtype("string") == "text"
    assert _bq_dtype("Int64") == "bigint"


# ── value coercion ───────────────────────────────────────────────


def test_coerce_decimal_to_float() -> None:
    from decimal import Decimal

    assert _coerce_value(Decimal("1.5")) == 1.5


def test_coerce_other_types_passthrough() -> None:
    assert _coerce_value(42) == 42
    assert _coerce_value("x") == "x"
    assert _coerce_value(None) is None


# ── construction validation ──────────────────────────────────────


def _src(
    *, meta: dict | None = None, creds: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        dialect="bigquery",
        connection_meta=meta or {},
        _credentials=creds or {},
    )


def test_engine_requires_project() -> None:
    with pytest.raises(ValueError, match="'project'"):
        BigQueryEngine(
            _src(
                meta={"dataset": "analytics"},
                creds={"service_account_json": "{}"},
            )
        )


def test_engine_requires_dataset() -> None:
    with pytest.raises(ValueError, match="'dataset'"):
        BigQueryEngine(
            _src(
                meta={"project": "p"},
                creds={"service_account_json": "{}"},
            )
        )


def test_engine_requires_service_account_json() -> None:
    with pytest.raises(ValueError, match="service_account_json"):
        BigQueryEngine(
            _src(meta={"project": "p", "dataset": "d"}, creds={})
        )


def test_engine_default_location_is_us() -> None:
    engine = BigQueryEngine(
        _src(
            meta={"project": "p", "dataset": "d"},
            creds={"service_account_json": "{}"},
        )
    )
    assert engine._location == "US"


def test_engine_honours_explicit_location() -> None:
    engine = BigQueryEngine(
        _src(
            meta={
                "project": "p", "dataset": "d", "location": "EU",
            },
            creds={"service_account_json": "{}"},
        )
    )
    assert engine._location == "EU"


# ── validate_readonly delegates to sqlglot bigquery dialect ──────


def _engine() -> BigQueryEngine:
    return BigQueryEngine(
        _src(
            meta={"project": "p", "dataset": "d"},
            creds={"service_account_json": "{}"},
        )
    )


def test_validate_select_passes() -> None:
    assert _engine().validate_readonly(
        "SELECT * FROM `p.d.orders`"
    ).ok


def test_validate_insert_rejected() -> None:
    assert not _engine().validate_readonly(
        "INSERT INTO `p.d.orders` (id) VALUES (1)"
    ).ok


def test_validate_export_data_rejected() -> None:
    """BigQuery's data-exfiltration vector — must not pass the
    read-only validator."""
    # sqlglot may or may not parse EXPORT DATA as a known statement;
    # either way our validator should treat anything that isn't a
    # SELECT as rejected.
    assert not _engine().validate_readonly(
        "EXPORT DATA OPTIONS(uri='gs://bucket/*') "
        "AS SELECT * FROM `p.d.orders`"
    ).ok


# ── registry contract ────────────────────────────────────────────


def test_engine_registered_under_bigquery() -> None:
    from app.engines import register_all
    from app.engines.registry import DIALECT_REGISTRY

    register_all()
    assert "bigquery" in DIALECT_REGISTRY
