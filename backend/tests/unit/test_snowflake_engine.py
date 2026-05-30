"""Phase 28 — Snowflake engine adapter.

snowflake-connector-python isn't bundled in the unit-test env, so we
test the pure-Python helpers (dtype mapping, validation gating, the
construction-side checks for missing meta / creds) without touching
the connector. The execute path is exercised through the existing
sqlglot validator which IS installed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engines.snowflake import (
    _SF_TYPE_CODES,
    SnowflakeEngine,
    _coerce_value,
    _sf_type_code_to_dtype,
    _snowflake_dtype,
)


# ── dtype mapping ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dtype,expected",
    [
        ("NUMBER", "decimal"),
        ("DECIMAL", "decimal"),
        ("NUMERIC", "decimal"),
        ("INT", "bigint"),
        ("INTEGER", "bigint"),
        ("BIGINT", "bigint"),
        ("SMALLINT", "bigint"),
        ("TINYINT", "bigint"),
        ("BYTEINT", "bigint"),
        ("FLOAT", "double"),
        ("FLOAT4", "double"),
        ("FLOAT8", "double"),
        ("DOUBLE", "double"),
        ("REAL", "double"),
        ("VARCHAR", "text"),
        ("CHAR", "text"),
        ("STRING", "text"),
        ("TEXT", "text"),
        ("BINARY", "text"),
        ("VARBINARY", "text"),
        ("TIMESTAMP_NTZ", "timestamp"),
        ("TIMESTAMP_LTZ", "timestamp"),
        ("TIMESTAMP_TZ", "timestamp"),
        ("DATE", "date"),
        ("TIME", "timestamp"),
        ("BOOLEAN", "boolean"),
        ("VARIANT", "json"),
        ("OBJECT", "json"),
        ("ARRAY", "json"),
        ("GEOGRAPHY", "unknown"),  # not in our coarse map
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_snowflake_dtype_mapping(dtype, expected) -> None:
    assert _snowflake_dtype(dtype) == expected


def test_snowflake_dtype_case_insensitive() -> None:
    assert _snowflake_dtype("varchar") == "text"
    assert _snowflake_dtype("Number") == "decimal"


# ── type-code (DB-API) mapping ───────────────────────────────────


def test_sf_type_code_bigint() -> None:
    assert _sf_type_code_to_dtype(0) == "bigint"


def test_sf_type_code_text() -> None:
    assert _sf_type_code_to_dtype(2) == "text"


def test_sf_type_code_timestamp_variants() -> None:
    assert _sf_type_code_to_dtype(6) == "timestamp"  # LTZ
    assert _sf_type_code_to_dtype(7) == "timestamp"  # TZ
    assert _sf_type_code_to_dtype(8) == "timestamp"  # NTZ


def test_sf_type_code_unknown_falls_through() -> None:
    assert _sf_type_code_to_dtype(999) == "unknown"
    assert _sf_type_code_to_dtype(None) == "unknown"
    assert _sf_type_code_to_dtype("not-a-number") == "unknown"


def test_sf_type_code_table_has_expected_entries() -> None:
    """Lock the table shape so a future "let's add more types"
    edit doesn't silently break dtype routing."""
    for k in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):
        assert k in _SF_TYPE_CODES


# ── value coercion ───────────────────────────────────────────────


def test_coerce_decimal_to_float() -> None:
    from decimal import Decimal

    assert _coerce_value(Decimal("3.14")) == 3.14


def test_coerce_passes_through_other_types() -> None:
    assert _coerce_value(42) == 42
    assert _coerce_value("hello") == "hello"
    assert _coerce_value(None) is None
    assert _coerce_value(True) is True


# ── construction validation ──────────────────────────────────────


def _source(
    *, meta: dict | None = None, creds: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        dialect="snowflake",
        connection_meta=meta or {},
        _credentials=creds or {},
    )


def test_engine_rejects_missing_meta() -> None:
    with pytest.raises(ValueError, match="connection_meta missing"):
        SnowflakeEngine(_source(meta={}, creds={"user": "u", "password": "p"}))


def test_engine_rejects_missing_warehouse() -> None:
    with pytest.raises(ValueError, match="warehouse"):
        SnowflakeEngine(
            _source(
                meta={"account": "a", "database": "d", "schema": "s"},
                creds={"user": "u", "password": "p"},
            )
        )


def test_engine_rejects_missing_user() -> None:
    with pytest.raises(ValueError, match="'user'"):
        SnowflakeEngine(
            _source(
                meta={
                    "account": "a", "warehouse": "w",
                    "database": "d", "schema": "s",
                },
                creds={"password": "p"},
            )
        )


def test_engine_rejects_missing_password_and_private_key() -> None:
    with pytest.raises(ValueError, match="password.*private_key"):
        SnowflakeEngine(
            _source(
                meta={
                    "account": "a", "warehouse": "w",
                    "database": "d", "schema": "s",
                },
                creds={"user": "u"},
            )
        )


def test_engine_accepts_password_auth() -> None:
    engine = SnowflakeEngine(
        _source(
            meta={
                "account": "abc12345.eu-central-1",
                "warehouse": "WH",
                "database": "DB",
                "schema": "PUBLIC",
                "role": "READ_ONLY",
            },
            creds={"user": "alice", "password": "secret"},
        )
    )
    assert engine.dialect == "snowflake"
    assert engine._account == "abc12345.eu-central-1"
    assert engine._role == "READ_ONLY"


def test_engine_accepts_private_key_auth() -> None:
    engine = SnowflakeEngine(
        _source(
            meta={
                "account": "a", "warehouse": "w",
                "database": "d", "schema": "s",
            },
            creds={
                "user": "alice",
                "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
            },
        )
    )
    assert engine._password is None
    assert engine._private_key is not None


# ── validate_readonly delegates to sqlglot ───────────────────────


def test_validate_select_passes() -> None:
    engine = SnowflakeEngine(
        _source(
            meta={
                "account": "a", "warehouse": "w",
                "database": "d", "schema": "s",
            },
            creds={"user": "u", "password": "p"},
        )
    )
    out = engine.validate_readonly("SELECT * FROM orders")
    assert out.ok


def test_validate_insert_rejected() -> None:
    engine = SnowflakeEngine(
        _source(
            meta={
                "account": "a", "warehouse": "w",
                "database": "d", "schema": "s",
            },
            creds={"user": "u", "password": "p"},
        )
    )
    out = engine.validate_readonly("INSERT INTO orders VALUES (1)")
    assert not out.ok


def test_validate_copy_into_location_rejected() -> None:
    """Snowflake's data-exfiltration vector — must not slip past
    the read-only validator."""
    engine = SnowflakeEngine(
        _source(
            meta={
                "account": "a", "warehouse": "w",
                "database": "d", "schema": "s",
            },
            creds={"user": "u", "password": "p"},
        )
    )
    out = engine.validate_readonly(
        "COPY INTO @my_stage FROM orders"
    )
    assert not out.ok


# ── registry contract ────────────────────────────────────────────


def test_engine_registered_under_snowflake() -> None:
    from app.engines import register_all
    from app.engines.registry import DIALECT_REGISTRY

    register_all()
    assert "snowflake" in DIALECT_REGISTRY
