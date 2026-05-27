from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncmy

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


# System schemas to exclude from introspection. Mirrors the Postgres
# adapter's `_SYSTEM_SCHEMAS`; these are MySQL's built-in databases that
# should never appear in a user's workspace bundle.
_SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")


# MySQL protocol type codes -> normalized dtype strings used by the
# UI/answer-writer. Codes come from the C/Python driver protocol
# (see `asyncmy.constants.FIELD_TYPE`). We only need coarse buckets:
# numeric vs floating vs decimal vs timestamp vs bool vs string —
# the schema bundle has the authoritative DDL types.
_DTYPE_MAP: dict[int, str] = {
    1: "bigint",    # TINY
    2: "bigint",    # SHORT
    3: "bigint",    # LONG
    8: "bigint",    # LONGLONG
    9: "bigint",    # INT24
    4: "double",    # FLOAT
    5: "double",    # DOUBLE
    246: "numeric", # NEWDECIMAL
    7: "timestamp", # TIMESTAMP
    10: "timestamp", # DATE
    11: "timestamp", # TIME
    12: "timestamp", # DATETIME
    14: "timestamp", # NEWDATE
    16: "bool",     # BIT
    245: "string",  # JSON
    247: "string",  # ENUM
    250: "string",  # TINY_BLOB
    251: "string",  # MEDIUM_BLOB
    252: "string",  # LONG_BLOB
    253: "string",  # BLOB (VAR_STRING in some refs)
}


def _mysql_dtype(code: int) -> str:
    """Map a MySQL protocol type code (cursor.description[i][1]) to a
    normalized dtype string. Unknown codes fall through to "string"."""
    return _DTYPE_MAP.get(code, "string")


@register("mysql")
class MysqlEngine:
    dialect: Dialect = "mysql"

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
                f"MySQL connection missing keys: {sorted(missing)}"
            )
        self._db_name = meta["db_name"]
        self._dsn_kwargs: dict[str, Any] = {
            "host": meta["host"],
            "port": int(meta["port"]),
            "db": meta["db_name"],
            "user": meta["user"],
            "password": meta["password"],
        }
        # asyncmy expects an ssl object/dict or omission. Pass a truthy
        # marker dict when requested; production callers can extend this
        # to load CA bundles. None/False -> drop the kwarg entirely.
        if meta.get("ssl"):
            self._dsn_kwargs["ssl"] = {}

    async def _connect(self):
        return await asyncmy.connect(**self._dsn_kwargs)

    async def introspect_schema(self) -> SchemaBundle:
        conn = await self._connect()
        try:
            schemas_filter = ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS)
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT TABLE_SCHEMA, TABLE_NAME
                    FROM information_schema.tables
                    WHERE TABLE_TYPE = 'BASE TABLE'
                      AND TABLE_SCHEMA NOT IN ({schemas_filter})
                      AND TABLE_SCHEMA = %s
                    ORDER BY TABLE_SCHEMA, TABLE_NAME
                    """,
                    (self._db_name,),
                )
                table_rows = await cur.fetchall()

            tables: list[TableMeta] = []
            for tschema, tname in table_rows:
                # Columns. COLUMN_KEY = 'PRI' indicates the column is part
                # of the primary key (composite PKs surface as multiple
                # 'PRI' rows).
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY
                        FROM information_schema.columns
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY ORDINAL_POSITION
                        """,
                        (tschema, tname),
                    )
                    col_rows = await cur.fetchall()

                cols: list[ColumnMeta] = [
                    ColumnMeta(
                        name=cname,
                        data_type=dtype,
                        nullable=(is_nullable == "YES"),
                        is_pk=(col_key == "PRI"),
                    )
                    for (cname, dtype, is_nullable, col_key) in col_rows
                ]

                # Foreign keys via key_column_usage. The join on
                # REFERENCED_TABLE_NAME IS NOT NULL filters to FK rows
                # only (PK/UNIQUE constraints leave it NULL).
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT
                          COLUMN_NAME           AS from_col,
                          REFERENCED_TABLE_SCHEMA AS to_schema,
                          REFERENCED_TABLE_NAME AS to_table,
                          REFERENCED_COLUMN_NAME AS to_col
                        FROM information_schema.key_column_usage
                        WHERE TABLE_SCHEMA = %s
                          AND TABLE_NAME = %s
                          AND REFERENCED_TABLE_NAME IS NOT NULL
                        """,
                        (tschema, tname),
                    )
                    fk_rows = await cur.fetchall()

                fks: list[ForeignKeyMeta] = []
                for from_col, to_schema, to_table, to_col in fk_rows:
                    fks.append(
                        ForeignKeyMeta(
                            from_columns=[from_col],
                            to_table=f"{to_schema}.{to_table}",
                            to_columns=[to_col],
                        )
                    )
                    for c in cols:
                        if c.name == from_col:
                            c.fk_to = f"{to_schema}.{to_table}.{to_col}"

                # Row count estimate from information_schema.tables.
                # TABLE_ROWS is approximate on InnoDB; that's fine — the
                # planner only needs order-of-magnitude.
                row_count: int | None = None
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT TABLE_ROWS
                            FROM information_schema.tables
                            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                            """,
                            (tschema, tname),
                        )
                        rc_row = await cur.fetchone()
                        if rc_row and rc_row[0] is not None:
                            row_count = int(rc_row[0])
                except Exception:
                    pass

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
        # ID columns are filled in by services.schema_profiler via the
        # ID heuristic; skip sampling them — values are meaningless and
        # potentially expensive to scan distinctly.
        if col.is_id:
            return ColumnSample()

        conn = await self._connect()
        try:
            # MySQL uses backticks for identifier quoting (the default
            # ANSI_QUOTES SQL mode is off in nearly all deployments).
            qt = f"`{table.schema}`.`{table.name}`"
            qc = f"`{col.name}`"

            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT COUNT(*), COUNT(DISTINCT {qc}) FROM {qt}"
                )
                row = await cur.fetchone()
            row_count = int(row[0]) if row else 0
            distinct_count = int(row[1]) if row else 0

            dt = col.data_type.lower()
            is_numeric = any(
                k in dt
                for k in ("int", "numeric", "real", "double", "float", "decimal")
            )
            is_textual = any(
                k in dt for k in ("text", "char", "varchar", "enum", "string")
            )

            is_categorical = is_textual or (
                row_count > 0
                and distinct_count <= 50
                and (distinct_count / max(row_count, 1)) < 0.05
            )

            if is_categorical:
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"SELECT {qc} FROM {qt} "
                            f"WHERE {qc} IS NOT NULL GROUP BY {qc} LIMIT 51"
                        )
                        rows = await cur.fetchall()
                    vals = [r[0] for r in rows]
                    return ColumnSample(
                        distinct_values=vals[:50],
                        distinct_truncated=(len(vals) > 50),
                    )
                except Exception:
                    return ColumnSample()

            if is_numeric:
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"SELECT MIN({qc}), MAX({qc}), AVG({qc}), STDDEV({qc}) FROM {qt}"
                        )
                        stats_row = await cur.fetchone()
                    stats: dict[str, float] = {}
                    if stats_row:
                        for k, idx in (("min", 0), ("max", 1), ("avg", 2), ("stddev", 3)):
                            v = stats_row[idx]
                            if v is not None:
                                stats[k] = float(v)
                    return ColumnSample(numeric_stats=stats)
                except Exception:
                    return ColumnSample()

            # Fallback: 5-sample + non-null count.
            try:
                async with conn.cursor() as cur:
                    await cur.execute(f"SELECT COUNT({qc}) FROM {qt}")
                    nn_row = await cur.fetchone()
                    await cur.execute(
                        f"SELECT {qc} FROM {qt} "
                        f"WHERE {qc} IS NOT NULL LIMIT 5"
                    )
                    sample_rows = await cur.fetchall()
                return ColumnSample(
                    non_null_count=int(nn_row[0]) if nn_row else None,
                    sample_rows=[r[0] for r in sample_rows],
                )
            except Exception:
                return ColumnSample()
        finally:
            await conn.close()

    def validate_readonly(self, sql: str) -> ValidationResult:
        # sqlglot already understands the MySQL dialect — no engine-side
        # branching needed (see CLAUDE.md: dialect logic lives in
        # engines/, not the validator).
        return validate_readonly(sql, dialect="mysql")

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        # Layer 2 of read-only defense: parse-level check. The validator
        # rejects DML/DDL and dangerous functions before we ever touch
        # the wire.
        result = self.validate_readonly(sql)
        if not result.ok:
            raise ValueError(
                f"read-only validation failed: "
                f"{[f.code for f in result.findings]}"
            )
        sql_to_run = result.rewritten_sql or sql

        async def _run() -> ResultSet:
            started = time.perf_counter()
            conn = await self._connect()
            try:
                async with conn.cursor() as cur:
                    # Layer 3a: session-level read-only transaction.
                    # MySQL 5.6.5+ supports this; older versions ignore
                    # the statement silently which is acceptable (the
                    # parse layer is the real gate).
                    await cur.execute("SET SESSION TRANSACTION READ ONLY")
                    # Layer 3b: server-enforced query timeout in ms.
                    # MAX_EXECUTION_TIME is a MySQL 5.7.8+ session var;
                    # asyncio.wait_for below is the outer safety net.
                    await cur.execute(
                        f"SET SESSION MAX_EXECUTION_TIME = {timeout_s * 1000}"
                    )
                    await asyncio.wait_for(
                        cur.execute(sql_to_run), timeout=timeout_s
                    )
                    # +1 row to detect truncation cleanly without a
                    # follow-up COUNT.
                    rows_raw = await cur.fetchmany(row_cap + 1)
                    description = cur.description or []
                    cols = [d[0] for d in description]
                    # description tuple: (name, type_code, ...). Map the
                    # protocol type_code to a normalized dtype string.
                    dtypes = [_mysql_dtype(d[1]) for d in description]
            finally:
                # Closing the connection resets the session, so we don't
                # need an explicit SET SESSION TRANSACTION READ WRITE.
                await conn.close()

            took_ms = int((time.perf_counter() - started) * 1000)
            truncated = len(rows_raw) > row_cap
            rows = [list(r) for r in rows_raw[:row_cap]]
            return ResultSet(
                columns=cols,
                dtypes=dtypes,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                took_ms=took_ms,
            )

        return await asyncio.wait_for(_run(), timeout=timeout_s + 2)

    async def aclose(self) -> None:
        # Connections are short-lived per call; nothing to clean up.
        return None
