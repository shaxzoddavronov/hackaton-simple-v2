"""Snowflake engine adapter (Phase 28).

Driver: ``snowflake-connector-python``. It's a sync driver — we wrap
each call in ``asyncio.to_thread`` so the agent's async path stays
clean. ``snowflake-sqlalchemy`` would let us go through SQLAlchemy
but the connector covers everything we need and avoids the extra
wheel.

Read-only enforcement (defense in depth, three layers):

  1. **Parse** — :func:`app.services.readonly_validator.validate_readonly`
     with ``dialect="snowflake"``. sqlglot ships a Snowflake parser so
     DML / DDL / TRUNCATE / COPY INTO LOCATION are all rejected before
     a single byte hits the wire.

  2. **Session** — every connection is opened with a freshly-bound
     warehouse + database + schema and the parameter
     ``query_tag='querymind-read'`` so Snowflake's history tab shows
     which queries we issued. We never call ``BEGIN`` — Snowflake's
     default is implicit autocommit + statement-level READ COMMITTED
     for SELECTs.

  3. **Runtime** — ``cursor.execute`` is wrapped in
     ``asyncio.wait_for(..., timeout=timeout_s + 2)`` so a runaway
     query in Snowflake's compute-on-demand model can't burn credits
     forever.

Numerics: Snowflake returns ``decimal.Decimal`` for NUMBER columns.
We coerce to ``float`` at fetch time, matching the Oracle / MSSQL
convention — the SSE serialiser doesn't need a Decimal encoder.

Connection meta shape::

    {
      "account":   "abc12345.eu-central-1",  // required, full account locator
      "warehouse": "ANALYTICS_WH",            // required
      "database":  "ANALYTICS",               // required
      "schema":    "PUBLIC",                  // required, dialect-default schema
      "role":      "READ_ONLY",               // optional
    }

Credentials (decrypted, attached as ``_credentials``):

    {
      "user":     "alice",      // required
      "password": "...",         // EITHER password ...
      "private_key": "<pem>",   // OR a PEM private key for key-pair auth
    }
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from app.engines.base import (
    ColumnMeta,
    ColumnSample,
    Dialect,
    ForeignKeyMeta,
    ResultSet,
    SchemaBundle,
    TableMeta,
    ValidationResult,
)
from app.engines.registry import register
from app.services.readonly_validator import validate_readonly


# Snowflake schemas we never expose to the planner — internal catalogs
# and the demo SNOWFLAKE_SAMPLE_DATA share that ships on every account.
_SYSTEM_SCHEMAS = ("INFORMATION_SCHEMA",)


def _coerce_value(v: Any) -> Any:
    """Convert Decimal → float so the SSE JSON serialiser doesn't
    need a custom encoder. Matches the Oracle / MSSQL convention."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _snowflake_dtype(data_type: str | None) -> str:
    """Normalise a ``INFORMATION_SCHEMA.COLUMNS.DATA_TYPE`` to one
    of the dtype strings the UI / answer writer understands.

    Snowflake's DATA_TYPE column carries ANSI names ("NUMBER",
    "VARCHAR", "TIMESTAMP_NTZ", "VARIANT", ...). We bucket the
    common ones; anything else falls through as ``"unknown"``.
    """
    if not data_type:
        return "unknown"
    dt = data_type.upper()
    if dt in ("NUMBER", "DECIMAL", "NUMERIC"):
        return "decimal"
    if dt in ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"):
        return "bigint"
    if dt in ("FLOAT", "DOUBLE", "REAL", "FLOAT4", "FLOAT8"):
        return "double"
    if dt in ("VARCHAR", "CHAR", "STRING", "TEXT", "BINARY", "VARBINARY"):
        return "text"
    if dt.startswith("TIMESTAMP"):
        return "timestamp"
    if dt == "DATE":
        return "date"
    if dt == "TIME":
        return "timestamp"
    if dt == "BOOLEAN":
        return "boolean"
    if dt in ("VARIANT", "OBJECT", "ARRAY"):
        return "json"
    return "unknown"


@register("snowflake")
class SnowflakeEngine:
    dialect: Dialect = "snowflake"

    def __init__(self, source) -> None:
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        required = {"account", "warehouse", "database", "schema"}
        missing = required - meta.keys()
        if missing:
            raise ValueError(
                f"Snowflake connection_meta missing keys: {sorted(missing)}"
            )
        if not creds.get("user"):
            raise ValueError("Snowflake credentials require 'user'")
        if not (creds.get("password") or creds.get("private_key")):
            raise ValueError(
                "Snowflake credentials require either 'password' or 'private_key'"
            )
        self._account: str = str(meta["account"])
        self._warehouse: str = str(meta["warehouse"])
        self._database: str = str(meta["database"])
        self._schema: str = str(meta["schema"])
        self._role: str | None = str(meta["role"]) if meta.get("role") else None
        self._user: str = str(creds["user"])
        self._password: str | None = (
            str(creds["password"]) if creds.get("password") else None
        )
        self._private_key: str | None = (
            str(creds["private_key"]) if creds.get("private_key") else None
        )

    def _connect_sync(self):
        # Local import so the snowflake-connector-python wheel is
        # only required when this engine is actually used.
        import snowflake.connector as sf

        kwargs: dict[str, Any] = {
            "account": self._account,
            "user": self._user,
            "warehouse": self._warehouse,
            "database": self._database,
            "schema": self._schema,
            "session_parameters": {"QUERY_TAG": "querymind-read"},
            "client_session_keep_alive": False,
            "autocommit": True,
        }
        if self._role:
            kwargs["role"] = self._role
        if self._password:
            kwargs["password"] = self._password
        else:
            # Key-pair auth — caller supplied PEM bytes.
            from cryptography.hazmat.primitives import serialization

            pk = serialization.load_pem_private_key(
                self._private_key.encode("utf-8"), password=None
            )
            kwargs["private_key"] = pk.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        return sf.connect(**kwargs)

    async def _connect(self):
        return await asyncio.to_thread(self._connect_sync)

    async def introspect_schema(self) -> SchemaBundle:
        def _run() -> SchemaBundle:
            conn = self._connect_sync()
            try:
                cur = conn.cursor()
                try:
                    # Restrict to the configured database + non-system
                    # schemas. Snowflake's INFORMATION_SCHEMA is per-
                    # database, so we don't need an explicit ``db.`` prefix.
                    cur.execute(
                        """
                        SELECT table_schema, table_name
                        FROM INFORMATION_SCHEMA.TABLES
                        WHERE table_type = 'BASE TABLE'
                          AND table_schema NOT IN (%s)
                        ORDER BY table_schema, table_name
                        """,
                        (",".join(_SYSTEM_SCHEMAS),),
                    )
                    table_rows = cur.fetchall()
                finally:
                    cur.close()

                tables: list[TableMeta] = []
                for tschema, tname in table_rows:
                    # Columns + PK info — one round-trip per table.
                    # For huge databases this is slow; v2 would
                    # batch but typical workspace connections are
                    # 10-100 tables.
                    cur = conn.cursor()
                    try:
                        cur.execute(
                            """
                            SELECT column_name, data_type, is_nullable
                            FROM INFORMATION_SCHEMA.COLUMNS
                            WHERE table_schema = %s AND table_name = %s
                            ORDER BY ordinal_position
                            """,
                            (tschema, tname),
                        )
                        col_rows = cur.fetchall()
                    finally:
                        cur.close()

                    # PK columns via TABLE_CONSTRAINTS + KEY_COLUMN_USAGE.
                    cur = conn.cursor()
                    try:
                        cur.execute(
                            """
                            SELECT kcu.column_name
                            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                              ON tc.constraint_name = kcu.constraint_name
                             AND tc.constraint_schema = kcu.constraint_schema
                            WHERE tc.constraint_type = 'PRIMARY KEY'
                              AND tc.table_schema = %s
                              AND tc.table_name = %s
                            """,
                            (tschema, tname),
                        )
                        pk_cols = {r[0] for r in cur.fetchall()}
                    finally:
                        cur.close()

                    cols: list[ColumnMeta] = [
                        ColumnMeta(
                            name=cname,
                            data_type=dtype,
                            nullable=(is_nullable == "YES"),
                            is_pk=(cname in pk_cols),
                        )
                        for (cname, dtype, is_nullable) in col_rows
                    ]
                    tables.append(
                        TableMeta(
                            schema=tschema,
                            name=tname,
                            columns=cols,
                            foreign_keys=[],  # Snowflake FKs are not enforced; skip introspection v1
                        )
                    )
                return SchemaBundle(dialect=self.dialect, tables=tables)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        return await asyncio.to_thread(_run)

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        # Sampling against Snowflake burns compute credits. Skip for
        # v1 — the schema bundle alone is enough context.
        return ColumnSample()

    def validate_readonly(self, sql: str) -> ValidationResult:
        # sqlglot ships a Snowflake parser; the standard validator
        # rejects DML/DDL + injects a LIMIT cap.
        return validate_readonly(sql, dialect="snowflake")

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        result = self.validate_readonly(sql)
        if not result.ok:
            raise ValueError(
                f"read-only validation failed: "
                f"{[f.code for f in result.findings]}"
            )
        sql_to_run = result.rewritten_sql or sql

        def _run() -> ResultSet:
            started = time.perf_counter()
            conn = self._connect_sync()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql_to_run)
                    description = cur.description or []
                    rows_raw = cur.fetchmany(row_cap + 1)
                finally:
                    cur.close()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            took_ms = int((time.perf_counter() - started) * 1000)
            cols = [d[0] for d in description]
            # Snowflake's description tuple: (name, type_code, ...).
            # type_code is a numeric constant (1=NUMBER, 2=FLOAT, 3=
            # VARCHAR, ...). We map the most common ones; unknowns
            # fall through as "unknown".
            dtypes = [
                _sf_type_code_to_dtype(d[1]) for d in description
            ]
            truncated = len(rows_raw) > row_cap
            rows = [
                [_coerce_value(v) for v in r]
                for r in rows_raw[:row_cap]
            ]
            return ResultSet(
                columns=cols,
                dtypes=dtypes,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                took_ms=took_ms,
            )

        return await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=timeout_s + 2
        )

    async def aclose(self) -> None:
        # Connections are short-lived per call; nothing to clean up.
        return None


# Snowflake DB-API type codes — see snowflake.connector.constants.
# We bucket the common subset; everything else degrades to "unknown".
_SF_TYPE_CODES = {
    0: "bigint",     # FIXED
    1: "double",     # REAL
    2: "text",       # TEXT
    3: "date",       # DATE
    4: "timestamp",  # TIMESTAMP
    5: "json",       # VARIANT
    6: "timestamp",  # TIMESTAMP_LTZ
    7: "timestamp",  # TIMESTAMP_TZ
    8: "timestamp",  # TIMESTAMP_NTZ
    9: "json",       # OBJECT
    10: "json",      # ARRAY
    11: "text",      # BINARY
    12: "timestamp", # TIME
    13: "boolean",   # BOOLEAN
}


def _sf_type_code_to_dtype(type_code: Any) -> str:
    if isinstance(type_code, int) and type_code in _SF_TYPE_CODES:
        return _SF_TYPE_CODES[type_code]
    return "unknown"
