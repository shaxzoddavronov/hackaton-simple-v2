"""BigQuery engine adapter (Phase 30).

Driver: ``google-cloud-bigquery``. Sync client wrapped in
``asyncio.to_thread``. BigQuery's REST API does have an async
client (``google-cloud-bigquery-storage``) but the sync one covers
everything we need without a second wheel.

Read-only enforcement (defense in depth, three layers):

  1. **Parse** — :func:`app.services.readonly_validator.validate_readonly`
     with ``dialect="bigquery"``. sqlglot ships a BigQuery parser
     so DML / DDL / EXPORT DATA / LOAD DATA / CALL are all rejected
     before any bytes hit the API.

  2. **Job config** — every query is submitted with
     ``QueryJobConfig(use_query_cache=True, dry_run=False)`` and we
     do NOT pass any destination table or write_disposition. The
     BigQuery server-side default is "create a temp result table",
     which is a read.

  3. **Runtime** — ``asyncio.wait_for(timeout_s + 5)`` so a runaway
     query (or a slot-starved project) can't burn $ forever. BQ is
     billed per byte scanned — we cap row_cap on the engine side too.

Numerics: BigQuery's NUMERIC / BIGNUMERIC come back as ``Decimal``;
TIMESTAMPs as ``datetime``. Decimal → float at fetch time (matches
Oracle / Snowflake / MSSQL pattern).

Connection meta shape::

    {
      "project":  "my-gcp-project",     // billing + default project
      "dataset":  "analytics",          // default dataset
      "location": "EU",                  // optional, "US" by default
    }

Credentials (decrypted, attached as ``_credentials``):

    {
      "service_account_json": "<raw JSON key>",
    }

We deliberately don't support ADC (Application Default Credentials)
because the agent runs in a container where Google's metadata server
isn't reachable. Service-account JSON is the operational fit.
"""
from __future__ import annotations

import asyncio
import json
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


def _coerce_value(v: Any) -> Any:
    """Decimal → float; datetime stays as-is (the SSE serializer
    handles datetimes). Matches the Oracle / Snowflake convention."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _bq_dtype(field_type: str | None) -> str:
    """Map ``INFORMATION_SCHEMA.COLUMNS.data_type`` to the dtype
    strings the UI / answer-writer understand.

    BigQuery types: STRING, BYTES, INTEGER (or INT64), FLOAT
    (FLOAT64), NUMERIC, BIGNUMERIC, BOOLEAN (BOOL), TIMESTAMP,
    DATE, TIME, DATETIME, GEOGRAPHY, JSON, STRUCT, ARRAY.
    """
    if not field_type:
        return "unknown"
    t = field_type.upper()
    # Strip array/struct wrappers — INFORMATION_SCHEMA gives them
    # as "ARRAY<INT64>" or "STRUCT<...>". We surface the inner
    # element type when the wrapper is a simple array.
    if t.startswith("ARRAY<") and t.endswith(">"):
        return "array"
    if t.startswith("STRUCT<"):
        return "object"
    if t in ("INT64", "INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT"):
        return "bigint"
    if t in ("FLOAT64", "FLOAT", "REAL", "DOUBLE"):
        return "double"
    if t in ("NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"):
        return "decimal"
    if t in ("STRING", "BYTES"):
        return "text"
    if t in ("BOOL", "BOOLEAN"):
        return "boolean"
    if t == "DATE":
        return "date"
    if t in ("TIMESTAMP", "DATETIME", "TIME"):
        return "timestamp"
    if t == "JSON":
        return "json"
    if t == "GEOGRAPHY":
        return "text"
    return "unknown"


@register("bigquery")
class BigQueryEngine:
    dialect: Dialect = "bigquery"

    def __init__(self, source) -> None:
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        if not meta.get("project"):
            raise ValueError(
                "BigQuery connection_meta missing 'project'"
            )
        if not meta.get("dataset"):
            raise ValueError(
                "BigQuery connection_meta missing 'dataset'"
            )
        if not creds.get("service_account_json"):
            raise ValueError(
                "BigQuery credentials missing 'service_account_json' "
                "(raw JSON of a GCP service account key)"
            )
        self._project: str = str(meta["project"])
        self._dataset: str = str(meta["dataset"])
        self._location: str = str(meta.get("location") or "US")
        self._sa_json: str = str(creds["service_account_json"])

    def _build_client(self):
        """Construct a ``google.cloud.bigquery.Client``. Local
        imports keep the SDK off the import path for installs that
        don't use BigQuery."""
        from google.cloud import bigquery
        from google.oauth2 import service_account

        sa_info = json.loads(self._sa_json)
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=[
                "https://www.googleapis.com/auth/bigquery.readonly",
                "https://www.googleapis.com/auth/cloud-platform.read-only",
            ],
        )
        return bigquery.Client(
            project=self._project,
            credentials=creds,
            location=self._location,
        )

    async def introspect_schema(self) -> SchemaBundle:
        def _run() -> SchemaBundle:
            client = self._build_client()
            try:
                # INFORMATION_SCHEMA is per-dataset; fully qualify
                # via the region prefix so location works.
                table_sql = (
                    f"SELECT table_name "
                    f"FROM `{self._project}.{self._dataset}."
                    "INFORMATION_SCHEMA.TABLES` "
                    "WHERE table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
                table_rows = list(client.query(table_sql).result())

                tables: list[TableMeta] = []
                for row in table_rows:
                    tname = row["table_name"]
                    col_sql = (
                        "SELECT column_name, data_type, is_nullable "
                        f"FROM `{self._project}.{self._dataset}."
                        "INFORMATION_SCHEMA.COLUMNS` "
                        f"WHERE table_name = '{tname}' "
                        "ORDER BY ordinal_position"
                    )
                    cols_raw = list(client.query(col_sql).result())
                    cols: list[ColumnMeta] = [
                        ColumnMeta(
                            name=r["column_name"],
                            data_type=r["data_type"],
                            nullable=(r["is_nullable"] == "YES"),
                        )
                        for r in cols_raw
                    ]
                    tables.append(
                        TableMeta(
                            schema=self._dataset,
                            name=tname,
                            columns=cols,
                            foreign_keys=[],
                        )
                    )
                return SchemaBundle(dialect=self.dialect, tables=tables)
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        return await asyncio.to_thread(_run)

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        # BigQuery is billed per byte scanned. Sampling burns money
        # for marginal planner value. Skip.
        return ColumnSample()

    def validate_readonly(self, sql: str) -> ValidationResult:
        return validate_readonly(sql, dialect="bigquery")

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
            from google.cloud import bigquery

            started = time.perf_counter()
            client = self._build_client()
            try:
                job_config = bigquery.QueryJobConfig(
                    use_query_cache=True,
                    dry_run=False,
                )
                job = client.query(
                    sql_to_run,
                    job_config=job_config,
                    location=self._location,
                )
                result_iter = job.result(max_results=row_cap + 1)

                # Pull column metadata from the query job's schema.
                fields = result_iter.schema
                cols = [f.name for f in fields]
                dtypes = [_bq_dtype(f.field_type) for f in fields]

                rows_raw: list[list[Any]] = []
                for row in result_iter:
                    rows_raw.append(
                        [_coerce_value(row[c]) for c in cols]
                    )

                took_ms = int((time.perf_counter() - started) * 1000)
                truncated = len(rows_raw) > row_cap
                rows = rows_raw[:row_cap]
                return ResultSet(
                    columns=cols,
                    dtypes=dtypes,
                    rows=rows,
                    row_count=len(rows),
                    truncated=truncated,
                    took_ms=took_ms,
                )
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        return await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=timeout_s + 5
        )

    async def aclose(self) -> None:
        return None
