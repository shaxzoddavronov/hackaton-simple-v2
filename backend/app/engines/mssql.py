"""Microsoft SQL Server engine adapter.

Driver: ``aioodbc`` (an asyncio wrapper around ``pyodbc``). It needs an
ODBC driver available on the host — typically ``ODBC Driver 18 for SQL
Server`` on Windows / modern Linux, or Driver 17 on older boxes. Callers
can override the driver name via ``connection_meta["driver"]``.

Read-only enforcement (defense in depth, three layers):

1. **Parse** — :func:`app.services.readonly_validator.validate_readonly`
   with ``dialect="tsql"``; sqlglot understands T-SQL grammar and rejects
   DML/DDL before we ever hit the wire. The validator also injects a row
   cap via a ``LIMIT`` node — sqlglot renders that as ``SELECT TOP n``
   when emitting tsql, so the wire query lands in proper T-SQL form.

2. **Session** — ``SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED``.
   SQL Server has no Postgres-style ``BEGIN READ ONLY`` transaction
   mode; the read-uncommitted level is the closest equivalent that
   neither takes nor waits on locks, which is appropriate for ad-hoc
   analytical SELECTs against a possibly busy OLTP database.

3. **Runtime** — ``SET QUERY_GOVERNOR_COST_LIMIT n`` kills queries whose
   estimated cost (in arbitrary optimizer units) exceeds the limit. We
   pass ``timeout_s`` directly; the unit mismatch with seconds is
   acceptable because ``asyncio.wait_for`` is the actual wall-clock
   ceiling.

Numerics: SQL Server returns ``decimal.Decimal`` for ``NUMERIC``,
``DECIMAL``, ``MONEY`` and ``SMALLMONEY`` columns. We coerce those to
``float`` at fetch time so the SSE JSON serializer doesn't need a custom
encoder (matching the same convention used by the Oracle adapter for
``NUMBER``).
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import aioodbc

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


# System schemas to skip during introspection. ``dbo`` is the default
# user schema in SQL Server and IS exposed; only the truly internal
# schemas are filtered. (``sys`` holds the catalog views, and
# ``INFORMATION_SCHEMA`` is just SQL Server's standard wrapper around
# them — neither belongs in a user's workspace bundle.)
_SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA")


def _mssql_dtype(data_type: str | None) -> str:
    """Map an ``INFORMATION_SCHEMA.COLUMNS.DATA_TYPE`` value to a
    normalized dtype string used by the UI / answer-writer.

    Unknown types fall through to ``"unknown"``. Coarse buckets only —
    the schema bundle has the authoritative DDL type string anyway.
    """
    if not data_type:
        return "unknown"
    dt = data_type.lower()

    # Integer family. ``int`` is a substring of ``smallint``/``tinyint``/
    # ``bigint`` so the membership check covers all four at once.
    if dt in ("bigint", "int", "smallint", "tinyint"):
        return "bigint"
    if dt in ("float", "real"):
        return "double"
    if dt in ("decimal", "numeric", "money", "smallmoney"):
        return "decimal"
    if dt in ("nvarchar", "varchar"):
        return "varchar"
    if dt in ("nchar", "char", "text", "ntext"):
        return "text"
    if dt in ("datetime", "datetime2", "smalldatetime", "datetimeoffset"):
        return "timestamp"
    if dt == "date":
        return "date"
    if dt == "time":
        return "timestamp"
    if dt == "bit":
        return "boolean"
    if dt == "uniqueidentifier":
        return "uuid"
    return "unknown"


def _coerce_value(v: Any) -> Any:
    """Convert ``Decimal`` to ``float``; pass everything else through.

    SQL Server returns ``Decimal`` for NUMERIC/DECIMAL/MONEY which the
    JSON encoder in the SSE layer doesn't handle natively. Datetimes are
    fine — the existing serializer already handles ``datetime`` objects.
    """
    if isinstance(v, Decimal):
        return float(v)
    return v


def _build_dsn(meta: dict[str, Any]) -> str:
    """Build the ODBC DSN string used to open the aioodbc connection.

    Required keys in ``meta``: ``host``, ``db_name``, ``user``,
    ``password``. ``port`` defaults to 1433. ``driver`` overrides the
    default ``ODBC Driver 18 for SQL Server`` — useful on Linux boxes
    that ship Driver 17.
    """
    driver = meta.get("driver", "ODBC Driver 18 for SQL Server")
    host = meta["host"]
    port = int(meta.get("port", 1433))
    database = meta["db_name"]
    user = meta["user"]
    password = meta["password"]
    # ``TrustServerCertificate=yes`` is the pragmatic default for
    # internal/self-hosted SQL Server boxes whose certs aren't in the
    # local trust store. ``Encrypt=optional`` lets the server choose:
    # encrypted if supported, plaintext if not. Callers who care about
    # strict TLS can pass their own ``driver`` value with extra
    # attributes appended (we don't currently surface that knob).
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        "TrustServerCertificate=yes;"
        "Encrypt=optional"
    )


@register("mssql")
class MssqlEngine:
    dialect: Dialect = "mssql"

    def __init__(self, source) -> None:
        # ``source`` is duck-typed: anything with connection_meta + an
        # optional ``_credentials`` dict works. In production it's a
        # WorkspaceConnection ORM row; tests pass SimpleNamespace.
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        meta.update(creds)
        required = {"host", "db_name", "user", "password"}
        missing = required - meta.keys()
        if missing:
            raise ValueError(
                f"SQL Server connection missing keys: {sorted(missing)}"
            )
        self._db_name: str = meta["db_name"]
        self._dsn: str = _build_dsn(meta)

    async def _connect(self):
        # aioodbc returns a real connection (not a pool) on ``connect``.
        # Connections are short-lived per call to match the other
        # adapters' pattern.
        return await aioodbc.connect(dsn=self._dsn, autocommit=True)

    async def introspect_schema(self) -> SchemaBundle:
        conn = await self._connect()
        try:
            schemas_filter = ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS)
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT TABLE_SCHEMA, TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_TYPE = 'BASE TABLE'
                      AND TABLE_SCHEMA NOT IN ({schemas_filter})
                    ORDER BY TABLE_SCHEMA, TABLE_NAME
                    """
                )
                table_rows = await cur.fetchall()

            tables: list[TableMeta] = []
            for tschema, tname in table_rows:
                # Columns. INFORMATION_SCHEMA gives us a portable view
                # of name / type / nullability per column.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                        ORDER BY ORDINAL_POSITION
                        """,
                        tschema,
                        tname,
                    )
                    col_rows = await cur.fetchall()

                # Primary-key columns. ``INFORMATION_SCHEMA`` exposes a
                # constraint→column join that handles composite PKs.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT kcu.COLUMN_NAME
                        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                          ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                         AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                         AND tc.TABLE_NAME = kcu.TABLE_NAME
                        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                          AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
                        """,
                        tschema,
                        tname,
                    )
                    pk_rows = await cur.fetchall()
                pk_cols = {r[0] for r in pk_rows}

                cols: list[ColumnMeta] = [
                    ColumnMeta(
                        name=cname,
                        data_type=dtype,
                        nullable=(is_nullable == "YES"),
                        is_pk=(cname in pk_cols),
                    )
                    for (cname, dtype, is_nullable) in col_rows
                ]

                # Foreign keys via ``sys.foreign_keys`` + the column
                # join table. ``sys.tables`` / ``sys.schemas`` resolve
                # the qualified name of the referenced table.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT
                          c_from.name   AS from_col,
                          s_to.name     AS to_schema,
                          t_to.name     AS to_table,
                          c_to.name     AS to_col
                        FROM sys.foreign_keys fk
                        JOIN sys.foreign_key_columns fkc
                          ON fkc.constraint_object_id = fk.object_id
                        JOIN sys.tables   t_from
                          ON t_from.object_id = fk.parent_object_id
                        JOIN sys.schemas  s_from
                          ON s_from.schema_id = t_from.schema_id
                        JOIN sys.columns  c_from
                          ON c_from.object_id = fkc.parent_object_id
                         AND c_from.column_id = fkc.parent_column_id
                        JOIN sys.tables   t_to
                          ON t_to.object_id = fk.referenced_object_id
                        JOIN sys.schemas  s_to
                          ON s_to.schema_id = t_to.schema_id
                        JOIN sys.columns  c_to
                          ON c_to.object_id = fkc.referenced_object_id
                         AND c_to.column_id = fkc.referenced_column_id
                        WHERE s_from.name = ? AND t_from.name = ?
                        ORDER BY fkc.constraint_column_id
                        """,
                        tschema,
                        tname,
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

                # Row count estimate from sys.dm_db_partition_stats.
                # Fast, lock-free, and accurate enough for the planner's
                # order-of-magnitude needs. index_id IN (0, 1) covers
                # heaps (0) and clustered indexes (1) — every base row
                # appears in exactly one of those.
                row_count: int | None = None
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT SUM(row_count)
                            FROM sys.dm_db_partition_stats
                            WHERE object_id = OBJECT_ID(?)
                              AND index_id IN (0, 1)
                            """,
                            f"{tschema}.{tname}",
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
            # T-SQL uses bracketed identifiers; this preserves names
            # that collide with reserved words or contain spaces.
            qt = f"[{table.schema}].[{table.name}]"
            qc = f"[{col.name}]"

            dt = col.data_type.lower()
            is_numeric = any(
                k in dt
                for k in (
                    "int",
                    "decimal",
                    "numeric",
                    "float",
                    "real",
                    "money",
                )
            )
            is_textual = any(
                k in dt for k in ("char", "text")
            )
            is_temporal = any(
                k in dt for k in ("date", "time")
            )

            # Skip temporal sampling — distinct counts on date/time are
            # rarely useful for the planner prompt.
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
                        await cur.execute(
                            f"SELECT DISTINCT TOP 51 {qc} FROM {qt} "
                            f"WHERE {qc} IS NOT NULL"
                        )
                        rows = await cur.fetchall()
                    vals = [_coerce_value(r[0]) for r in rows]
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
                            f"SELECT MIN({qc}), MAX({qc}), AVG(CAST({qc} AS FLOAT)), "
                            f"STDEV(CAST({qc} AS FLOAT)) FROM {qt}"
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
                        f"SELECT TOP 5 {qc} FROM {qt} "
                        f"WHERE {qc} IS NOT NULL"
                    )
                    sample_rows = await cur.fetchall()
                return ColumnSample(
                    non_null_count=int(nn_row[0]) if nn_row else None,
                    sample_rows=[_coerce_value(r[0]) for r in sample_rows],
                )
            except Exception:
                return ColumnSample()
        finally:
            await conn.close()

    def validate_readonly(self, sql: str) -> ValidationResult:
        # sqlglot already understands the T-SQL grammar — no engine-side
        # branching needed (see CLAUDE.md: dialect logic lives in
        # engines/, not the validator). sqlglot renders the injected
        # LIMIT cap as ``SELECT TOP n`` when emitting tsql, so the wire
        # query stays in proper T-SQL form.
        return validate_readonly(sql, dialect="tsql")

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        # Layer 2 of read-only defense: parse-level check. The validator
        # rejects DML/DDL and dangerous functions before we ever touch
        # the wire, and injects ``TOP <row_cap>`` if missing.
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
                    # Layer 3a: lowest isolation level — neither takes
                    # nor waits on locks. SQL Server has no true
                    # ``READ ONLY`` transaction mode like Postgres; this
                    # is the closest equivalent that's safe for ad-hoc
                    # analytics over a live OLTP database.
                    await cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED"
                    )
                    # Layer 3b: server-side query governor. Kills any
                    # query whose estimated cost (optimizer units, not
                    # seconds) exceeds the limit before execution.
                    # asyncio.wait_for below is the actual wall-clock
                    # ceiling.
                    await cur.execute(
                        f"SET QUERY_GOVERNOR_COST_LIMIT {int(timeout_s)}"
                    )
                    await cur.execute(sql_to_run)
                    # +1 row to detect truncation cleanly without a
                    # follow-up COUNT.
                    rows_raw = await cur.fetchmany(row_cap + 1)
                    description = cur.description or []
                    cols = [d[0] for d in description]
                    # description tuple: (name, type_code, display_size,
                    # internal_size, precision, scale, null_ok). pyodbc
                    # uses Python type objects (e.g. ``int``, ``str``,
                    # ``decimal.Decimal``) for the type_code slot, which
                    # don't carry the SQL Server DDL name. Bucket them
                    # by Python type so the UI gets something sensible.
                    dtypes = [_py_type_to_dtype(d[1]) for d in description]
            finally:
                # Closing the connection ends the session implicitly.
                await conn.close()

            took_ms = int((time.perf_counter() - started) * 1000)
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

        return await asyncio.wait_for(_run(), timeout=timeout_s + 2)

    async def aclose(self) -> None:
        # Connections are short-lived per call; nothing to clean up.
        return None


def _py_type_to_dtype(type_code: Any) -> str:
    """Map the Python type returned by pyodbc's ``cursor.description``
    to a normalized dtype string.

    pyodbc / aioodbc don't expose the underlying SQL Server type code in
    the description tuple — they put a Python ``type`` object there
    (e.g. ``<class 'int'>``, ``<class 'decimal.Decimal'>``). We bucket
    by that, which is coarse but matches what the rest of the agent
    pipeline actually needs.
    """
    if type_code is None:
        return "unknown"
    try:
        name = type_code.__name__
    except AttributeError:
        return "unknown"
    if name in ("int",):
        return "bigint"
    if name in ("float",):
        return "double"
    if name == "Decimal":
        # Coerced to float at fetch time — surface as ``double``.
        return "double"
    if name in ("bool",):
        return "boolean"
    if name in ("datetime", "date", "time"):
        return "timestamp"
    if name in ("str",):
        return "varchar"
    if name in ("bytes", "bytearray", "memoryview"):
        return "bytes"
    if name == "UUID":
        return "uuid"
    return "unknown"
