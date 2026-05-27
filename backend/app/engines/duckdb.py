"""DuckDB engine adapter.

DuckDB is an in-process analytical SQL engine — think SQLite for OLAP.
No server, no network: ``import duckdb`` and open a file (or ``:memory:``)
gives you a full SQL surface.

Read-only enforcement layers (mirrors ``sqlite.py``):

1. **Parse** — :func:`app.services.readonly_validator.validate_readonly`
   with ``dialect="duckdb"`` rejects DDL/DML at the sqlglot AST level.
2. **Runtime** — connections are opened with ``read_only=True`` whenever
   the underlying path is on-disk. DuckDB itself blocks any write at the
   engine layer when this flag is set.
3. **Outer** — :func:`asyncio.wait_for` adds a hard timeout ceiling.

Known relaxation: ``:memory:`` databases cannot be opened in read-only
mode (DuckDB needs RW access to create the schema). The :class:`DuckdbEngine`
falls back to opening RW for ``:memory:`` paths so tests can seed an
in-process database. Path connection_meta in production should always
point at an on-disk file.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import duckdb

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


_SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")


def _duckdb_dtype(desc_entry: Any) -> str:
    """Map a DuckDB type description (string or DuckDBPyType) to a portable name.

    The dispatch is case-insensitive substring matching against the type's
    string form. Order matters: check INT before DECIMAL because DuckDB
    doesn't mix the two, but later checks are mutually exclusive anyway.
    """
    s = str(desc_entry).upper()
    if "INT" in s:
        return "bigint"
    if "DOUBLE" in s or "FLOAT" in s or "REAL" in s:
        return "double"
    if "DECIMAL" in s or "NUMERIC" in s:
        return "numeric"
    if "DATE" in s or "TIMESTAMP" in s:
        return "timestamp"
    if "BOOL" in s:
        return "bool"
    return "string"


@register("duckdb")
class DuckdbEngine:
    dialect: Dialect = "duckdb"

    def __init__(self, source) -> None:
        # See PostgresEngine for the ``source`` duck-type contract.
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        meta.update(creds)
        self._path = meta.get("path")
        if not self._path:
            raise ValueError("DuckDB connection_meta must include 'path'")
        # DuckDB requires read-write on :memory: because the schema can
        # only be created inside the connection that opened it. For
        # on-disk files we get full read-only enforcement at the engine
        # layer in addition to the AST validator.
        self._read_only = self._path != ":memory:"

    def _connect_sync(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(database=self._path, read_only=self._read_only)

    async def _connect(self) -> duckdb.DuckDBPyConnection:
        return await asyncio.to_thread(self._connect_sync)

    async def introspect_schema(self) -> SchemaBundle:
        def _run() -> SchemaBundle:
            conn = self._connect_sync()
            try:
                schemas_filter = ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS)
                table_rows = conn.execute(
                    f"""
                    SELECT table_schema, table_name
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                      AND table_schema NOT IN ({schemas_filter})
                    ORDER BY table_schema, table_name
                    """
                ).fetchall()

                # Pull explicit FK declarations once and bucket per table.
                # ``duckdb_constraints()`` exposes columns we need:
                # schema_name, table_name, constraint_type,
                # constraint_column_names (list), referenced_table (str),
                # referenced_column_names (list).
                fk_rows: list[tuple] = []
                try:
                    fk_rows = conn.execute(
                        """
                        SELECT schema_name, table_name,
                               constraint_column_names, referenced_table,
                               referenced_column_names
                        FROM duckdb_constraints()
                        WHERE constraint_type = 'FOREIGN KEY'
                        """
                    ).fetchall()
                except Exception:
                    # ``duckdb_constraints()`` is best-effort; never let it
                    # break introspection.
                    fk_rows = []

                fks_by_table: dict[tuple[str, str], list[tuple]] = {}
                for tschema, tname, from_cols, to_table, to_cols in fk_rows:
                    fks_by_table.setdefault((tschema, tname), []).append(
                        (list(from_cols or []), to_table, list(to_cols or []))
                    )

                tables: list[TableMeta] = []
                for tschema, tname in table_rows:
                    col_rows = conn.execute(
                        """
                        SELECT column_name, data_type, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = ? AND table_name = ?
                        ORDER BY ordinal_position
                        """,
                        [tschema, tname],
                    ).fetchall()

                    cols: list[ColumnMeta] = [
                        ColumnMeta(
                            name=cn,
                            data_type=dt or "",
                            nullable=(nn == "YES"),
                        )
                        for cn, dt, nn in col_rows
                    ]

                    fks: list[ForeignKeyMeta] = []
                    for from_cols, to_table, to_cols in fks_by_table.get(
                        (tschema, tname), []
                    ):
                        if not from_cols or not to_table or not to_cols:
                            continue
                        qualified_to = f"{tschema}.{to_table}"
                        fks.append(
                            ForeignKeyMeta(
                                from_columns=list(from_cols),
                                to_table=qualified_to,
                                to_columns=list(to_cols),
                            )
                        )
                        # Stamp fk_to on the first column for quick lookup
                        # (matches the sqlite engine convention).
                        for c in cols:
                            if c.name == from_cols[0]:
                                c.fk_to = f"{qualified_to}.{to_cols[0]}"

                    row_count: int | None = None
                    try:
                        rc_row = conn.execute(
                            f'SELECT COUNT(*) FROM "{tschema}"."{tname}"'
                        ).fetchone()
                        if rc_row:
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
                conn.close()

        return await asyncio.to_thread(_run)

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        def _run() -> ColumnSample:
            conn = self._connect_sync()
            try:
                qt = f'"{table.schema}"."{table.name}"'
                qc = f'"{col.name}"'

                if col.is_id:
                    return ColumnSample()

                row_count = 0
                distinct_count = 0
                try:
                    r = conn.execute(
                        f"SELECT COUNT(*), COUNT(DISTINCT {qc}) FROM {qt}"
                    ).fetchone()
                    if r:
                        row_count = int(r[0])
                        distinct_count = int(r[1])
                except Exception:
                    return ColumnSample()

                dt = col.data_type.lower()
                is_numeric = any(
                    k in dt
                    for k in ("int", "real", "float", "double", "numeric", "decimal")
                )
                is_textual = any(
                    k in dt for k in ("char", "varchar", "text", "string")
                )

                is_categorical = is_textual or (
                    row_count > 0
                    and distinct_count <= 50
                    and (distinct_count / max(row_count, 1)) < 0.05
                )
                if is_categorical:
                    try:
                        vals = [
                            r[0]
                            for r in conn.execute(
                                f"SELECT {qc} FROM {qt} "
                                f"WHERE {qc} IS NOT NULL GROUP BY {qc} LIMIT 51"
                            ).fetchall()
                        ]
                        return ColumnSample(
                            distinct_values=vals[:50],
                            distinct_truncated=(len(vals) > 50),
                        )
                    except Exception:
                        pass

                if is_numeric:
                    try:
                        r = conn.execute(
                            f"SELECT MIN({qc}), MAX({qc}), AVG({qc}), "
                            f"STDDEV({qc}) FROM {qt}"
                        ).fetchone()
                        stats: dict[str, float] = {}
                        if r:
                            for key, idx in (
                                ("min", 0),
                                ("max", 1),
                                ("avg", 2),
                                ("stddev", 3),
                            ):
                                v = r[idx]
                                if v is not None:
                                    stats[key] = float(v)
                        return ColumnSample(numeric_stats=stats)
                    except Exception:
                        pass

                # Generic fallback.
                non_null: int | None = None
                sample_rows: list[Any] = []
                try:
                    r = conn.execute(
                        f"SELECT COUNT({qc}) FROM {qt}"
                    ).fetchone()
                    if r:
                        non_null = int(r[0])
                    sample_rows = [
                        r[0]
                        for r in conn.execute(
                            f"SELECT {qc} FROM {qt} "
                            f"WHERE {qc} IS NOT NULL LIMIT 5"
                        ).fetchall()
                    ]
                except Exception:
                    pass
                return ColumnSample(
                    non_null_count=non_null, sample_rows=sample_rows
                )
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    def validate_readonly(self, sql: str) -> ValidationResult:
        return validate_readonly(sql, dialect="duckdb")

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        val = self.validate_readonly(sql)
        if not val.ok:
            raise ValueError(
                "Refusing to execute: "
                + "; ".join(f.message for f in val.findings)
            )
        sql_to_run = val.rewritten_sql or sql

        def _run() -> tuple[list[tuple], list, float]:
            conn = self._connect_sync()
            try:
                started = time.perf_counter()
                cur = conn.execute(sql_to_run)
                description = list(cur.description) if cur.description else []
                rows_raw = cur.fetchmany(row_cap + 1)
                took = time.perf_counter() - started
                return description, rows_raw, took
            finally:
                conn.close()

        description, rows_raw, took = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=timeout_s + 1
        )
        columns = [d[0] for d in description]
        dtypes = [_duckdb_dtype(d[1]) for d in description]
        truncated = len(rows_raw) > row_cap
        rows = [list(r) for r in (rows_raw[:row_cap] if truncated else rows_raw)]
        return ResultSet(
            columns=columns,
            dtypes=dtypes,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            took_ms=int(took * 1000),
        )

    async def aclose(self) -> None:
        # Short-lived connections per call — nothing to clean up.
        return None
