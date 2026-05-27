from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncpg

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


_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


@register("postgres")
class PostgresEngine:
    dialect: Dialect = "postgres"

    def __init__(self, source) -> None:
        # ``source`` is duck-typed: anything with connection_meta + an
        # optional ``_credentials`` dict works. In production it's a
        # WorkspaceConnection ORM row; tests pass SimpleNamespace.
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        meta.update(creds)
        required = {"host", "port", "db_name", "user", "password"}
        missing = required - meta.keys()
        if missing:
            raise ValueError(
                f"Postgres connection missing keys: {sorted(missing)}"
            )
        self._dsn_kwargs = {
            "host": meta["host"],
            "port": int(meta["port"]),
            "database": meta["db_name"],
            "user": meta["user"],
            "password": meta["password"],
            "ssl": "require" if meta.get("ssl") else None,
        }
        # asyncpg complains about unknown kwargs, drop None ssl
        if self._dsn_kwargs["ssl"] is None:
            self._dsn_kwargs.pop("ssl")

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(**self._dsn_kwargs)

    async def introspect_schema(self) -> SchemaBundle:
        conn = await self._connect()
        try:
            schemas_filter = ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS)
            table_rows = await conn.fetch(
                f"""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema NOT IN ({schemas_filter})
                ORDER BY table_schema, table_name
                """
            )

            tables: list[TableMeta] = []
            for tr in table_rows:
                tschema, tname = tr["table_schema"], tr["table_name"]
                col_rows = await conn.fetch(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = $1 AND table_name = $2
                    ORDER BY ordinal_position
                    """,
                    tschema,
                    tname,
                )

                # Primary keys
                pk_rows = await conn.fetch(
                    """
                    SELECT a.attname AS column_name
                    FROM   pg_index i
                    JOIN   pg_attribute a ON a.attrelid = i.indrelid
                                          AND a.attnum = ANY(i.indkey)
                    WHERE  i.indrelid = $1::regclass AND i.indisprimary
                    """,
                    f"{tschema}.{tname}",
                )
                pk_cols = {r["column_name"] for r in pk_rows}

                cols: list[ColumnMeta] = [
                    ColumnMeta(
                        name=cr["column_name"],
                        data_type=cr["data_type"],
                        nullable=(cr["is_nullable"] == "YES"),
                        is_pk=(cr["column_name"] in pk_cols),
                    )
                    for cr in col_rows
                ]

                # Foreign keys
                fk_rows = await conn.fetch(
                    """
                    SELECT
                      kcu.column_name AS from_col,
                      ccu.table_schema AS to_schema,
                      ccu.table_name AS to_table,
                      ccu.column_name AS to_col
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage AS ccu
                      ON ccu.constraint_name = tc.constraint_name
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema = $1 AND tc.table_name = $2
                    """,
                    tschema,
                    tname,
                )
                fks: list[ForeignKeyMeta] = []
                for fr in fk_rows:
                    fks.append(
                        ForeignKeyMeta(
                            from_columns=[fr["from_col"]],
                            to_table=f"{fr['to_schema']}.{fr['to_table']}",
                            to_columns=[fr["to_col"]],
                        )
                    )
                    for c in cols:
                        if c.name == fr["from_col"]:
                            c.fk_to = f"{fr['to_schema']}.{fr['to_table']}.{fr['to_col']}"

                # Row count estimate from pg_class (cheap; reltuples is approximate)
                rc_row = await conn.fetchrow(
                    "SELECT reltuples::bigint AS rc FROM pg_class WHERE oid = $1::regclass",
                    f"{tschema}.{tname}",
                )
                row_count = int(rc_row["rc"]) if rc_row and rc_row["rc"] is not None else None

                tables.append(
                    TableMeta(
                        schema=tschema,
                        name=tname,
                        columns=cols,
                        foreign_keys=fks,
                        row_count_estimate=row_count,
                    )
                )

            return SchemaBundle(dialect=self.dialect, tables=tables)
        finally:
            await conn.close()

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        conn = await self._connect()
        try:
            qt = f'"{table.schema}"."{table.name}"'
            qc = f'"{col.name}"'

            if col.is_id:
                return ColumnSample()

            row = await conn.fetchrow(
                f"SELECT COUNT(*) AS rc, COUNT(DISTINCT {qc}) AS dc FROM {qt}"
            )
            row_count = int(row["rc"]) if row else 0
            distinct_count = int(row["dc"]) if row else 0

            dt = col.data_type.lower()
            is_numeric = any(
                k in dt
                for k in ("int", "numeric", "real", "double", "float", "decimal")
            )
            is_textual = any(k in dt for k in ("text", "char"))

            is_categorical = is_textual or (
                row_count > 0
                and distinct_count <= 50
                and (distinct_count / max(row_count, 1)) < 0.05
            )

            if is_categorical:
                rows = await conn.fetch(
                    f"SELECT {qc} AS v FROM {qt} "
                    f"WHERE {qc} IS NOT NULL GROUP BY {qc} LIMIT 51"
                )
                vals = [r["v"] for r in rows]
                return ColumnSample(
                    distinct_values=vals[:50],
                    distinct_truncated=(len(vals) > 50),
                )

            if is_numeric:
                stats_row = await conn.fetchrow(
                    f"SELECT MIN({qc}) AS mn, MAX({qc}) AS mx, "
                    f"AVG({qc})::float AS av, STDDEV({qc})::float AS sd FROM {qt}"
                )
                stats: dict[str, float] = {}
                if stats_row:
                    for k, alias in (("min", "mn"), ("max", "mx"), ("avg", "av"), ("stddev", "sd")):
                        v = stats_row[alias]
                        if v is not None:
                            stats[k] = float(v)
                return ColumnSample(numeric_stats=stats)

            # Fallback: 5-sample + non-null count
            nn_row = await conn.fetchrow(
                f"SELECT COUNT({qc}) AS nn FROM {qt}"
            )
            sample_rows = await conn.fetch(
                f"SELECT {qc} AS v FROM {qt} "
                f"WHERE {qc} IS NOT NULL LIMIT 5"
            )
            return ColumnSample(
                non_null_count=int(nn_row["nn"]) if nn_row else None,
                sample_rows=[r["v"] for r in sample_rows],
            )
        finally:
            await conn.close()

    def validate_readonly(self, sql: str) -> ValidationResult:
        return validate_readonly(sql, dialect="postgres")

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        async def _run() -> ResultSet:
            conn = await self._connect()
            try:
                started = time.perf_counter()
                # Three-layer read-only enforcement at runtime.
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        f"SET LOCAL statement_timeout = '{timeout_s * 1000}ms'"
                    )
                    await conn.execute(
                        "SET LOCAL idle_in_transaction_session_timeout = '15s'"
                    )
                    raw = await conn.fetch(sql)
                took_ms = int((time.perf_counter() - started) * 1000)

                columns: list[str] = list(raw[0].keys()) if raw else []
                truncated = len(raw) > row_cap
                rows = [
                    [row[c] for c in columns]
                    for row in (raw[:row_cap] if truncated else raw)
                ]
                return ResultSet(
                    columns=columns,
                    dtypes=[""] * len(columns),
                    rows=rows,
                    row_count=len(rows),
                    truncated=truncated,
                    took_ms=took_ms,
                )
            finally:
                await conn.close()

        return await asyncio.wait_for(_run(), timeout=timeout_s + 2)

    async def aclose(self) -> None:
        return None
