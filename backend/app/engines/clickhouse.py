"""ClickHouse ``QueryEngine`` adapter.

ClickHouse is a column-oriented analytical database. It speaks SQL but
diverges from the Postgres/MySQL conventions in a few important ways:

  * **Schemas** — ClickHouse calls them *databases*. We expose them via
    :attr:`TableMeta.schema` for parity with the SQL adapters. Only
    tables inside the configured ``db_name`` are introspected; system
    databases (``system``, ``INFORMATION_SCHEMA``, ``information_schema``)
    are filtered out.
  * **No foreign keys** — the engine has no FK concept, so every
    ``TableMeta`` lands with ``foreign_keys=[]``.
  * **Primary keys** — there is no DDL-level PK; instead the table's
    ``ORDER BY`` clause defines the sparse primary index. We read
    ``system.tables.primary_key`` (a comma-separated string) and flag
    those columns as ``is_pk=True``.
  * **Nullable types** are wrapped: e.g. ``Nullable(String)``. We unwrap
    the outer ``Nullable(...)`` for :attr:`ColumnMeta.data_type` and
    surface the wrapper as ``nullable=True``.

Read-only enforcement (defense in depth):

1. **Parse** — :func:`app.services.readonly_validator.validate_readonly`
   with ``dialect="clickhouse"``; sqlglot understands the dialect and
   rejects DML/DDL before we ever hit the wire.
2. **Runtime** — every ``execute`` call passes ClickHouse's session
   ``readonly=2`` setting (SELECT-only, but session-level settings like
   ``max_execution_time`` may still be set). We pair that with
   ``max_execution_time`` (server-side hard ceiling), ``max_result_rows``
   = ``row_cap + 1`` (so we can detect truncation cleanly), and
   ``result_overflow_mode='break'`` (truncate, don't raise).
3. **Outer ceiling** — :func:`asyncio.wait_for` wraps the whole call at
   ``timeout_s + 1`` in case the driver itself wedges.

Driver: the official :mod:`clickhouse_connect` async client speaks the
HTTP interface on port 8123 by default. ClickHouse-connect supports
``{name:Type}`` server-side placeholders (e.g. ``{db:String}``,
``{col:Identifier}``) for parameterized queries — we use those instead
of string interpolation wherever a user-controlled identifier or value
appears in introspection / sampling.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from clickhouse_connect import get_async_client

from app.engines.base import (
    ColumnMeta,
    ColumnSample,
    Dialect,
    ResultSet,
    SchemaBundle,
    TableMeta,
    ValidationResult,
)
from app.engines.registry import register
from app.services.readonly_validator import validate_readonly


# ClickHouse system databases we never want to expose to the planner.
_SYSTEM_SCHEMAS = ("system", "INFORMATION_SCHEMA", "information_schema")


def _unwrap_nullable(ch_type: str) -> tuple[str, bool]:
    """Strip a ``Nullable(...)`` wrapper, returning (inner_type, nullable).

    ClickHouse encodes nullability inside the column type string rather
    than as a separate flag, e.g. ``Nullable(String)`` or
    ``Nullable(Decimal(18, 4))``. Non-nullable columns appear as their
    raw type.
    """
    t = ch_type.strip()
    if t.startswith("Nullable(") and t.endswith(")"):
        return t[len("Nullable("):-1].strip(), True
    return t, False


def _ch_dtype(ch_type: Any) -> str:
    """Map a ClickHouse column type to one of the normalized dtype
    buckets used by the UI / answer-writer.

    The exact attribute on a ``ClickHouseType`` object varies by driver
    version (sometimes ``.name``, sometimes only ``str(t)``). We fall
    back to ``str(t)`` and run a simple substring match — coarse but
    sufficient for downstream consumers, which use the schema bundle's
    DDL types as the authoritative source.
    """
    name = getattr(ch_type, "name", None) or str(ch_type)
    # Order matters: check Float / Decimal before the generic Int test
    # because "UInt" obviously contains "Int" but "Float64" does not.
    if "Float" in name:
        return "double"
    if "Decimal" in name:
        return "numeric"
    if "Int" in name or "UInt" in name:
        return "bigint"
    if "DateTime" in name or "Date" in name:
        return "timestamp"
    if name == "Bool":
        return "bool"
    return "string"


@register("clickhouse")
class ClickhouseEngine:
    dialect: Dialect = "clickhouse"

    def __init__(self, source) -> None:
        # ``source`` is duck-typed: anything with connection_meta + an
        # optional ``_credentials`` dict works. In production it's a
        # WorkspaceConnection ORM row; tests pass SimpleNamespace.
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        meta.update(creds)
        required = {"host", "db_name"}
        missing = required - meta.keys()
        if missing:
            raise ValueError(
                f"ClickHouse connection missing keys: {sorted(missing)}"
            )
        self._db_name: str = meta["db_name"]
        self._client_kwargs: dict[str, Any] = {
            "host": meta["host"],
            "port": int(meta.get("port", 8123)),
            "database": meta["db_name"],
            "username": meta.get("user", "default"),
            "password": meta.get("password", ""),
            "secure": bool(meta.get("ssl", False)),
        }

    async def _connect(self):
        return await get_async_client(**self._client_kwargs)

    async def introspect_schema(self) -> SchemaBundle:
        client = await self._connect()
        try:
            # Tables in the configured database, skipping system DBs and
            # views (views are derived but otherwise harmless — the
            # planner can read them). We also fetch the primary_key
            # string in the same trip to avoid a second round-trip per
            # table.
            tbl_result = await client.query(
                """
                SELECT name, primary_key
                FROM system.tables
                WHERE database = {db:String}
                  AND database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')
                ORDER BY name
                """,
                parameters={"db": self._db_name},
            )
            table_rows: list[tuple[str, str]] = [
                (r[0], r[1] or "") for r in (tbl_result.result_rows or [])
            ]

            tables: list[TableMeta] = []
            for tname, pk_str in table_rows:
                pk_cols = {
                    p.strip() for p in pk_str.split(",") if p.strip()
                }

                col_result = await client.query(
                    """
                    SELECT name, type, default_kind
                    FROM system.columns
                    WHERE database = {db:String} AND table = {table:String}
                    ORDER BY position
                    """,
                    parameters={"db": self._db_name, "table": tname},
                )
                cols: list[ColumnMeta] = []
                for row in col_result.result_rows or []:
                    cname, ctype, _default_kind = row[0], row[1], row[2]
                    inner_type, is_nullable = _unwrap_nullable(str(ctype))
                    cols.append(
                        ColumnMeta(
                            name=cname,
                            data_type=inner_type,
                            nullable=is_nullable,
                            is_pk=(cname in pk_cols),
                        )
                    )

                # Row count estimate from system.tables.total_rows. This
                # is best-effort: some engines (e.g. Merge, Distributed)
                # report NULL. Run as a separate query so we can fail
                # softly.
                row_count: int | None = None
                try:
                    rc_result = await client.query(
                        """
                        SELECT total_rows FROM system.tables
                        WHERE database = {db:String} AND name = {table:String}
                        """,
                        parameters={"db": self._db_name, "table": tname},
                    )
                    rc_rows = rc_result.result_rows or []
                    if rc_rows and rc_rows[0][0] is not None:
                        row_count = int(rc_rows[0][0])
                except Exception:
                    pass

                tables.append(
                    TableMeta(
                        schema=self._db_name,
                        name=tname,
                        columns=cols,
                        # ClickHouse has no FK concept.
                        foreign_keys=[],
                        row_count_estimate=row_count,
                    )
                )

            return SchemaBundle(dialect=self.dialect, tables=tables)
        finally:
            await client.close()

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        # ID columns are flagged by services.schema_profiler via the ID
        # heuristic; skip sampling them — distinct counts on a unique
        # column are both meaningless and potentially expensive.
        if col.is_id:
            return ColumnSample()

        client = await self._connect()
        try:
            dt = col.data_type
            # Categorical buckets: String, LowCardinality(String),
            # FixedString(N), Enum8/Enum16. We probe the inner type
            # string from the bundle (already Nullable-unwrapped).
            is_textual = (
                "String" in dt
                or "FixedString" in dt
                or "Enum" in dt
            )
            is_numeric = (
                "Int" in dt
                or "UInt" in dt
                or "Float" in dt
                or "Decimal" in dt
            )

            if is_textual:
                try:
                    rs = await client.query(
                        "SELECT DISTINCT {col:Identifier} "
                        "FROM {db:Identifier}.{tbl:Identifier} LIMIT 51",
                        parameters={
                            "db": table.schema,
                            "tbl": table.name,
                            "col": col.name,
                        },
                    )
                    vals = [r[0] for r in (rs.result_rows or [])]
                    return ColumnSample(
                        distinct_values=vals[:50],
                        distinct_truncated=(len(vals) > 50),
                    )
                except Exception:
                    return ColumnSample()

            if is_numeric:
                try:
                    rs = await client.query(
                        "SELECT min({col:Identifier}), max({col:Identifier}), "
                        "avg({col:Identifier}), stddevPop({col:Identifier}) "
                        "FROM {db:Identifier}.{tbl:Identifier}",
                        parameters={
                            "db": table.schema,
                            "tbl": table.name,
                            "col": col.name,
                        },
                    )
                    rows = rs.result_rows or []
                    stats: dict[str, float] = {}
                    if rows:
                        row = rows[0]
                        for k, idx in (
                            ("min", 0),
                            ("max", 1),
                            ("avg", 2),
                            ("stddev", 3),
                        ):
                            v = row[idx] if idx < len(row) else None
                            if v is not None:
                                try:
                                    stats[k] = float(v)
                                except (TypeError, ValueError):
                                    pass
                    return ColumnSample(numeric_stats=stats)
                except Exception:
                    return ColumnSample()

            # DateTime, Array, Tuple, Map, UUID, IPv4/IPv6, etc. fall
            # through to the neutral sample. The planner only needs DDL
            # types for these; sampled values rarely help.
            return ColumnSample()
        except Exception:
            # Defensive catch-all so one bad column doesn't abort the
            # whole profile pass.
            return ColumnSample()
        finally:
            await client.close()

    def validate_readonly(self, sql: str) -> ValidationResult:
        # sqlglot understands the ClickHouse dialect — no engine-side
        # branching needed (see CLAUDE.md: dialect logic lives in
        # engines/, not the validator).
        return validate_readonly(sql, dialect="clickhouse")

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        # Layer 2 of read-only defense: parse-level check. The validator
        # rejects DML/DDL/multi-statement input before we ever touch the
        # wire.
        val = self.validate_readonly(sql)
        if not val.ok:
            raise ValueError(
                "Refusing to execute: "
                + "; ".join(f.message for f in val.findings)
            )
        sql_to_run = val.rewritten_sql or sql

        # Layer 3: server-enforced read-only + timeouts + row cap.
        #
        #   readonly=2          — SELECTs only, but SETTINGS in the
        #                         query are still allowed (1 is stricter).
        #   max_execution_time  — hard server-side ceiling, in seconds.
        #   max_result_rows     — row_cap + 1 so we can detect truncation
        #                         without a follow-up COUNT.
        #   result_overflow_mode='break' — truncate cleanly instead of
        #                         raising when the cap is hit.
        settings = {
            "readonly": 2,
            "max_execution_time": timeout_s,
            "max_result_rows": row_cap + 1,
            "result_overflow_mode": "break",
        }

        t0 = time.perf_counter()
        client = await self._connect()
        try:
            result = await asyncio.wait_for(
                client.query(sql_to_run, settings=settings),
                # Outer ceiling slightly higher than the server-side cap
                # so the driver gets a chance to surface ClickHouse's
                # own timeout error rather than us cancelling first.
                timeout=timeout_s + 1,
            )
        finally:
            await client.close()

        columns = list(result.column_names)
        dtypes = [_ch_dtype(t) for t in (result.column_types or [])]
        # Pad dtypes if for some driver-version reason column_types is
        # shorter than column_names.
        if len(dtypes) < len(columns):
            dtypes = dtypes + ["string"] * (len(columns) - len(dtypes))

        rows_raw = list(result.result_rows or [])
        truncated = len(rows_raw) > row_cap
        rows = [list(r) for r in rows_raw[:row_cap]]
        return ResultSet(
            columns=columns,
            dtypes=dtypes,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            took_ms=int((time.perf_counter() - t0) * 1000),
        )

    async def aclose(self) -> None:
        # Connections are short-lived per call; nothing to clean up.
        return None
