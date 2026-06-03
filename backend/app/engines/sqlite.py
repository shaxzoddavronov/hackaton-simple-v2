from __future__ import annotations

import asyncio
import time
from typing import Any

import aiosqlite

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


@register("sqlite")
class SqliteEngine:
    dialect: Dialect = "sqlite"

    def __init__(self, source) -> None:
        # See PostgresEngine for the ``source`` duck-type contract.
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        meta.update(creds)
        self._path = meta.get("path")
        if not self._path:
            raise ValueError("SQLite connection_meta must include 'path'")
        # Read-only URI form. For :memory: the read-only URI doesn't apply.
        if self._path == ":memory:":
            self._conn_uri = ":memory:"
            self._read_only_uri = False
        else:
            self._conn_uri = f"file:{self._path}?mode=ro"
            self._read_only_uri = True

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(
            self._conn_uri,
            uri=self._read_only_uri,
        )
        await conn.execute("PRAGMA query_only = ON;")
        return conn

    async def introspect_schema(self) -> SchemaBundle:
        conn = await self._connect()
        try:
            tables: list[TableMeta] = []
            async with conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ) as cur:
                table_names = [row[0] async for row in cur]

            for tname in table_names:
                cols: list[ColumnMeta] = []
                async with conn.execute(f"PRAGMA table_info({tname})") as cur:
                    async for row in cur:
                        # (cid, name, type, notnull, dflt_value, pk)
                        cols.append(
                            ColumnMeta(
                                name=row[1],
                                data_type=row[2] or "",
                                nullable=(row[3] == 0),
                                is_pk=(row[5] > 0),
                            )
                        )

                fks: list[ForeignKeyMeta] = []
                async with conn.execute(f"PRAGMA foreign_key_list({tname})") as cur:
                    async for row in cur:
                        # (id, seq, table, from, to, on_update, on_delete, match)
                        fks.append(
                            ForeignKeyMeta(
                                from_columns=[row[3]],
                                to_table=f"main.{row[2]}",
                                to_columns=[row[4]],
                            )
                        )
                        for c in cols:
                            if c.name == row[3]:
                                c.fk_to = f"main.{row[2]}.{row[4]}"

                row_count: int | None = None
                try:
                    async with conn.execute(
                        f"SELECT COUNT(*) FROM {tname}"
                    ) as cur:
                        rc = await cur.fetchone()
                        if rc:
                            row_count = int(rc[0])
                except Exception:
                    pass

                tables.append(
                    TableMeta(
                        schema="main",
                        name=tname,
                        columns=cols,
                        foreign_keys=fks,
                        row_count_estimate=row_count,
                    )
                )

            return SchemaBundle(dialect=self.dialect, tables=tables)
        finally:
            await conn.close()

    async def sample_column(self, table: TableMeta, col: ColumnMeta) -> ColumnSample:
        conn = await self._connect()
        try:
            return await _sample_column_generic(
                conn, table.schema, table.name, col, dialect="sqlite"
            )
        finally:
            await conn.close()

    def validate_readonly(self, sql: str) -> ValidationResult:
        return validate_readonly(sql, dialect="sqlite")

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        async def _run() -> ResultSet:
            conn = await self._connect()
            try:
                started = time.perf_counter()
                async with conn.execute(sql) as cur:
                    rows: list[list[Any]] = []
                    columns: list[str] = (
                        [d[0] for d in cur.description] if cur.description else []
                    )
                    truncated = False
                    fetched = 0
                    async for row in cur:
                        if fetched >= row_cap:
                            truncated = True
                            break
                        rows.append(list(row))
                        fetched += 1
                took_ms = int((time.perf_counter() - started) * 1000)
                # SQLite doesn't expose per-column dtype in cursor.description
                # (only column name lands there). Infer by scanning the
                # first non-null cell of each column — shared helper so
                # the rule matches what Mongo / ES get.
                from app.services.value_dtype import infer_column_dtypes

                dtypes = infer_column_dtypes(columns, rows)
                return ResultSet(
                    columns=columns,
                    dtypes=dtypes,
                    rows=rows,
                    row_count=len(rows),
                    truncated=truncated,
                    took_ms=took_ms,
                )
            finally:
                await conn.close()

        return await asyncio.wait_for(_run(), timeout=timeout_s)

    async def aclose(self) -> None:
        # Connections are short-lived per call; nothing to clean up.
        return None


async def _sample_column_generic(
    conn,
    schema: str,
    table: str,
    col: ColumnMeta,
    *,
    dialect: str,
) -> ColumnSample:
    """Shared sampler used by both SQLite and (in-process) tests.

    Heuristic rules per PLAN.md:
      - ID heuristic (name match, PK, unique monotonic int) -> skip value profiling
      - Categorical (string-ish OR distinct/row < 5% AND distinct <= 50) -> list distinct
      - Numeric -> MIN/MAX/AVG, plus STDDEV where dialect supports it
      - Other -> count non-null + 5-sample
    """
    qtable = f'"{table}"' if dialect == "postgres" else table
    qcol = f'"{col.name}"' if dialect == "postgres" else col.name

    if col.is_id:
        return ColumnSample()

    # distinct count (capped) + row count
    distinct_count = 0
    row_count = 0
    try:
        async with conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT {qcol}) FROM {qtable}"
        ) as cur:
            r = await cur.fetchone()
            if r:
                row_count = int(r[0])
                distinct_count = int(r[1])
    except Exception:
        return ColumnSample()

    dt = col.data_type.lower()
    is_numeric = any(
        k in dt for k in ("int", "real", "float", "double", "numeric", "decimal")
    )
    is_textual = any(k in dt for k in ("text", "char", "varchar", "string"))

    # Categorical branch
    is_categorical = is_textual or (
        row_count > 0
        and distinct_count <= 50
        and (distinct_count / max(row_count, 1)) < 0.05
    )
    if is_categorical:
        try:
            async with conn.execute(
                f"SELECT {qcol} FROM {qtable} "
                f"WHERE {qcol} IS NOT NULL GROUP BY {qcol} LIMIT 51"
            ) as cur:
                vals = [r[0] async for r in cur]
            return ColumnSample(
                distinct_values=vals[:50],
                distinct_truncated=(len(vals) > 50),
                non_null_count=None,
            )
        except Exception:
            pass

    if is_numeric:
        try:
            async with conn.execute(
                f"SELECT MIN({qcol}), MAX({qcol}), AVG({qcol}) FROM {qtable}"
            ) as cur:
                r = await cur.fetchone()
            stats: dict[str, float] = {}
            if r:
                if r[0] is not None:
                    stats["min"] = float(r[0])
                if r[1] is not None:
                    stats["max"] = float(r[1])
                if r[2] is not None:
                    stats["avg"] = float(r[2])
            return ColumnSample(numeric_stats=stats)
        except Exception:
            pass

    # Generic fallback: 5-sample + non-null count
    samples: list[Any] = []
    non_null: int | None = None
    try:
        async with conn.execute(
            f"SELECT COUNT({qcol}) FROM {qtable}"
        ) as cur:
            r = await cur.fetchone()
            if r:
                non_null = int(r[0])
        async with conn.execute(
            f"SELECT {qcol} FROM {qtable} "
            f"WHERE {qcol} IS NOT NULL LIMIT 5"
        ) as cur:
            samples = [r[0] async for r in cur]
    except Exception:
        pass
    return ColumnSample(non_null_count=non_null, sample_rows=samples)
