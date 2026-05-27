"""Oracle engine adapter.

Uses the ``oracledb`` driver in **thin mode** — pure Python, no Oracle
Instant Client install required. Mirrors the Postgres and MySQL adapters
1:1 for ergonomics; differences below are Oracle-specific.

Read-only enforcement (defense in depth, three layers):
    1. Parse — :func:`app.services.readonly_validator.validate_readonly`
       with ``dialect="oracle"``; sqlglot understands the Oracle grammar.
    2. Session — ``SET TRANSACTION READ ONLY`` issued before every query.
       Oracle honors this at the server side; any DML inside the same
       transaction raises ORA-01456.
    3. Runtime — :attr:`AsyncConnection.call_timeout` (milliseconds) as a
       per-statement server-enforced ceiling, plus an outer
       :func:`asyncio.wait_for` as a hard client-side guard.

Dtype mapping caveat: Oracle has only one numeric type (``NUMBER``),
which covers integers, fixed-point decimals, and floats. We bucket it as
``"double"`` because the python ``decimal.Decimal`` values returned by
oracledb serialize cleanly to JSON floats once
``oracledb.defaults.fetch_decimals = False`` is set (so all numerics come
back as ``float`` directly).

Identifiers are quoted with double quotes (Oracle SQL standard); this
preserves case sensitivity, which matters because Oracle stores
unquoted identifiers in UPPERCASE in its data dictionary.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import oracledb

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


# Return LOBs as native Python str/bytes rather than streaming LOB
# locators — much simpler for the agent pipeline, which never streams.
# Return NUMBER as float rather than decimal.Decimal so JSON serialization
# in the SSE layer doesn't need a custom encoder.
oracledb.defaults.fetch_lobs = False
oracledb.defaults.fetch_decimals = False


# Map oracledb DB_TYPE_* constants (cursor.description[i][1]) to the
# normalized dtype strings used by the UI/answer-writer. Coarse buckets
# only — the schema bundle has the authoritative DDL types.
_DTYPE_MAP: dict[Any, str] = {
    oracledb.DB_TYPE_NUMBER: "double",
    oracledb.DB_TYPE_BINARY_FLOAT: "double",
    oracledb.DB_TYPE_BINARY_DOUBLE: "double",
    oracledb.DB_TYPE_BINARY_INTEGER: "double",
    oracledb.DB_TYPE_VARCHAR: "string",
    oracledb.DB_TYPE_CHAR: "string",
    oracledb.DB_TYPE_NVARCHAR: "string",
    oracledb.DB_TYPE_NCHAR: "string",
    oracledb.DB_TYPE_LONG: "string",
    oracledb.DB_TYPE_LONG_NVARCHAR: "string",
    oracledb.DB_TYPE_CLOB: "string",
    oracledb.DB_TYPE_NCLOB: "string",
    oracledb.DB_TYPE_JSON: "string",
    oracledb.DB_TYPE_ROWID: "string",
    oracledb.DB_TYPE_UROWID: "string",
    oracledb.DB_TYPE_DATE: "timestamp",
    oracledb.DB_TYPE_TIMESTAMP: "timestamp",
    oracledb.DB_TYPE_TIMESTAMP_TZ: "timestamp",
    oracledb.DB_TYPE_TIMESTAMP_LTZ: "timestamp",
    oracledb.DB_TYPE_BOOLEAN: "bool",
}


def _oracle_dtype(desc_entry: Any) -> str:
    """Map a ``cursor.description`` entry to a normalized dtype string.

    The second tuple element from ``description`` is either an oracledb
    DbType constant or, in some edge cases, a class. Unknown codes fall
    through to ``"string"``.
    """
    code = desc_entry[1]
    return _DTYPE_MAP.get(code, "string")


@register("oracle")
class OracleEngine:
    dialect: Dialect = "oracle"

    def __init__(self, source) -> None:
        # ``source`` is duck-typed: anything with connection_meta + an
        # optional ``_credentials`` dict works. In production it's a
        # WorkspaceConnection ORM row; tests pass SimpleNamespace.
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        meta.update(creds)
        # Oracle terminology: callers may pass either ``db_name`` (the
        # repo-wide convention) or ``service_name`` (Oracle-native).
        if "db_name" not in meta and "service_name" in meta:
            meta["db_name"] = meta["service_name"]
        required = {"host", "port", "db_name", "user", "password"}
        missing = required - meta.keys()
        if missing:
            raise ValueError(
                f"Oracle connection missing keys: {sorted(missing)}"
            )
        # Build an Easy Connect / TNS-style DSN. Thin mode does NOT need
        # an Oracle client install or a tnsnames.ora file.
        dsn = oracledb.makedsn(
            meta["host"], int(meta["port"]), service_name=meta["db_name"]
        )
        self._connect_kwargs: dict[str, Any] = {
            "user": meta["user"],
            "password": meta["password"],
            "dsn": dsn,
        }
        # Cached USER schema (filled lazily by introspect_schema); Oracle
        # stores unquoted identifiers uppercased in the dictionary.
        self._schema_name: str | None = None

    async def _connect(self):
        # oracledb 2.x+ exposes a native async API via ``connect_async``.
        # If a future deployment downgrades to a wheel without it, the
        # fallback path runs the sync constructor in a worker thread —
        # the engine contract stays async at the boundary either way.
        if hasattr(oracledb, "connect_async"):
            return await oracledb.connect_async(**self._connect_kwargs)
        return await asyncio.to_thread(oracledb.connect, **self._connect_kwargs)

    async def introspect_schema(self) -> SchemaBundle:
        conn = await self._connect()
        try:
            # The current Oracle user is the schema we introspect.
            # ``user_*`` views show only the caller's own objects, so no
            # system-schema filter is needed (unlike Postgres/MySQL).
            async with conn.cursor() as cur:
                await cur.execute("SELECT USER FROM DUAL")
                row = await cur.fetchone()
                schema_name = row[0] if row else ""
            self._schema_name = schema_name

            # All base tables owned by the connected user.
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT table_name FROM user_tables ORDER BY table_name"
                )
                table_rows = await cur.fetchall()

            tables: list[TableMeta] = []
            for (tname,) in table_rows:
                # Columns.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT column_name, data_type, nullable
                        FROM user_tab_columns
                        WHERE table_name = :table_name
                        ORDER BY column_id
                        """,
                        table_name=tname,
                    )
                    col_rows = await cur.fetchall()

                # Primary-key column set. We pick the (single) PK
                # constraint for the table and join its columns.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT column_name
                        FROM user_cons_columns
                        WHERE constraint_name = (
                          SELECT constraint_name FROM user_constraints
                          WHERE table_name = :table_name
                            AND constraint_type = 'P'
                        )
                        """,
                        table_name=tname,
                    )
                    pk_rows = await cur.fetchall()
                pk_cols = {r[0] for r in pk_rows}

                cols: list[ColumnMeta] = [
                    ColumnMeta(
                        name=cname,
                        data_type=dtype,
                        # Oracle's ``user_tab_columns.nullable`` is 'Y'/'N'.
                        nullable=(is_nullable == "Y"),
                        is_pk=(cname in pk_cols),
                    )
                    for (cname, dtype, is_nullable) in col_rows
                ]

                # Foreign keys. Resolve the referenced PK columns via
                # ``r_constraint_name`` (the constraint on the parent
                # table) and join on ``position`` so composite FKs map
                # column-to-column.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT a.column_name   AS from_col,
                               c_pk.table_name AS to_table,
                               b.column_name   AS to_col
                        FROM user_cons_columns a
                        JOIN user_constraints c
                          ON a.constraint_name = c.constraint_name
                        JOIN user_constraints c_pk
                          ON c.r_constraint_name = c_pk.constraint_name
                        JOIN user_cons_columns b
                          ON c_pk.constraint_name = b.constraint_name
                         AND a.position = b.position
                        WHERE c.constraint_type = 'R'
                          AND c.table_name = :table_name
                        ORDER BY a.position
                        """,
                        table_name=tname,
                    )
                    fk_rows = await cur.fetchall()

                fks: list[ForeignKeyMeta] = []
                for from_col, to_table, to_col in fk_rows:
                    fks.append(
                        ForeignKeyMeta(
                            from_columns=[from_col],
                            to_table=f"{schema_name}.{to_table}",
                            to_columns=[to_col],
                        )
                    )
                    for c in cols:
                        if c.name == from_col:
                            c.fk_to = f"{schema_name}.{to_table}.{to_col}"

                # Row count estimate from ``user_tables.num_rows`` —
                # populated by the optimizer's last stats-gathering run
                # (may be None on a freshly-loaded table). Cheap; never
                # blocks. Planner only needs order-of-magnitude.
                row_count: int | None = None
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT num_rows
                            FROM user_tables
                            WHERE table_name = :table_name
                            """,
                            table_name=tname,
                        )
                        rc_row = await cur.fetchone()
                        if rc_row and rc_row[0] is not None:
                            row_count = int(rc_row[0])
                except Exception:
                    pass

                tables.append(
                    TableMeta(
                        schema=schema_name,
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
            # Oracle quotes identifiers with double quotes. Quoting
            # preserves case, which matters because unquoted names in
            # the dictionary are uppercase by convention.
            qt = f'"{table.schema}"."{table.name}"'
            qc = f'"{col.name}"'

            dt = col.data_type.upper()
            is_numeric = any(
                k in dt
                for k in (
                    "NUMBER",
                    "FLOAT",
                    "INTEGER",
                    "BINARY_FLOAT",
                    "BINARY_DOUBLE",
                    "DECIMAL",
                )
            )
            is_textual = any(
                k in dt for k in ("VARCHAR", "CHAR", "CLOB", "NCLOB")
            )
            is_temporal = "DATE" in dt or "TIMESTAMP" in dt or "INTERVAL" in dt

            # Skip temporal sampling entirely — distinct counts and
            # min/max on date/timestamp columns are unbounded and rarely
            # useful for the planner prompt.
            if is_temporal:
                return ColumnSample()

            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT COUNT(*), COUNT(DISTINCT {qc}) FROM {qt}"
                    )
                    row = await cur.fetchone()
                row_count = int(row[0]) if row else 0
                distinct_count = int(row[1]) if row else 0
            except Exception:
                return ColumnSample()

            is_categorical = is_textual or (
                row_count > 0
                and distinct_count <= 50
                and (distinct_count / max(row_count, 1)) < 0.05
            )

            if is_categorical:
                try:
                    async with conn.cursor() as cur:
                        # Oracle 12c+ supports ANSI FETCH FIRST. Pre-12c
                        # would need ``WHERE ROWNUM <= 51`` — 12c is from
                        # 2013 so the modern form is safe.
                        await cur.execute(
                            f"SELECT DISTINCT {qc} FROM {qt} "
                            f"WHERE {qc} IS NOT NULL "
                            f"FETCH FIRST 51 ROWS ONLY"
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
                        for k, idx in (
                            ("min", 0),
                            ("max", 1),
                            ("avg", 2),
                            ("stddev", 3),
                        ):
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
                        f"WHERE {qc} IS NOT NULL "
                        f"FETCH FIRST 5 ROWS ONLY"
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
        # sqlglot already understands the Oracle dialect — no engine-side
        # branching needed (see CLAUDE.md: dialect logic lives in
        # engines/, not the validator).
        return validate_readonly(sql, dialect="oracle")

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
                # Layer 3a: server-enforced per-statement timeout in ms.
                # Oracle's call_timeout aborts any single round-trip that
                # exceeds it with ORA-03136; asyncio.wait_for below is
                # the outer safety net.
                conn.call_timeout = timeout_s * 1000
                async with conn.cursor() as cur:
                    # Layer 3b: transaction-level read-only enforcement.
                    # Any DML attempted in the same transaction raises
                    # ORA-01456 server-side, even if the parse layer
                    # somehow let it through.
                    await cur.execute("SET TRANSACTION READ ONLY")
                    await cur.execute(sql_to_run)
                    # +1 row to detect truncation cleanly without a
                    # follow-up COUNT.
                    rows_raw = await cur.fetchmany(row_cap + 1)
                    description = cur.description or []
                    cols = [d[0] for d in description]
                    dtypes = [_oracle_dtype(d) for d in description]
            finally:
                # Closing the connection ends the read-only transaction;
                # no explicit ROLLBACK needed because we never committed.
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
