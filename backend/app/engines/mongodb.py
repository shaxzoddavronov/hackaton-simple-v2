"""MongoDB ``QueryEngine`` adapter.

Mongo is non-SQL but plugs into the existing :class:`QueryEngine`
Protocol with the same trick used for Elasticsearch:

  * The ``sql`` argument passed to :meth:`execute` and
    :meth:`validate_readonly` is actually a JSON envelope string of the
    shape::

        {
          "database":   "<db_name>",
          "collection": "<coll_name>",
          "pipeline":   [ { "$match": {...} }, { "$group": {...} }, ... ]
        }

    The planner is taught to emit it; the validator parses it back.

  * :meth:`execute` runs a single-collection
    ``db[coll].aggregate(pipeline)`` and flattens the resulting BSON
    documents into a tabular :class:`ResultSet` so chart_designer and
    answer_writer don't need to know we're talking to Mongo.

Security: every request goes through
:func:`app.services.mongo_readonly_validator.validate_mongo_query`. It
hard-rejects write stages (``$out``, ``$merge``), arbitrary code
operators (``$function``, ``$accumulator``, ``$where``), system stages,
and any reference to the ``admin`` / ``config`` / ``local`` databases or
``system.*`` collections.

Connection metadata shape (``WorkspaceConnection.connection_meta``):

  * ``host``: str — required.
  * ``db_name``: str — required.
  * ``port``: int, default 27017.
  * ``auth_source``: str, default = ``db_name``.
  * ``tls``: bool, default false.
  * ``replica_set``: str — optional.
  * ``direct_connection``: bool, default false.
  * ``server_selection_timeout_ms``: int, default 5000.

Credentials (decrypted, attached as ``_credentials`` dict by callers):

  * ``user`` + ``password`` — optional. Connect anonymously if absent.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus

from motor.motor_asyncio import AsyncIOMotorClient

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
from app.services.mongo_readonly_validator import validate_mongo_query


# Python-type → normalized dtype string used by the rest of the agent.
# Mongo is schemaless; this is best-effort based on sampled documents.
_PY_TO_DTYPE: dict[type, str] = {
    bool: "boolean",
    int: "bigint",
    float: "double",
    str: "string",
    bytes: "string",
    datetime: "timestamp",
    date: "timestamp",
    Decimal: "numeric",
    list: "array",
    dict: "object",
}


def _python_type_name(value: Any) -> str:
    """Best-effort dtype label for a sampled Mongo value."""
    if value is None:
        return "null"
    for cls, name in _PY_TO_DTYPE.items():
        if isinstance(value, cls):
            return name
    # ObjectId / Decimal128 / UUID etc. — fall back to the class name.
    return type(value).__name__.lower()


@register("mongodb")
class MongoEngine:
    dialect: Dialect = "mongodb"

    def __init__(self, source) -> None:
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}

        host = meta.get("host")
        db_name = meta.get("db_name")
        if not host or not db_name:
            raise ValueError(
                "MongoDB connection_meta must include 'host' and 'db_name'"
            )

        self._db_name: str = db_name
        self._meta = meta
        self._creds = creds

        uri = self._build_uri(meta, creds)
        self._client: AsyncIOMotorClient = AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=int(
                meta.get("server_selection_timeout_ms", 5000)
            ),
            appname="QueryMind",
        )
        self._db = self._client[db_name]

    # ── URI / client setup ──────────────────────────────────────────

    @staticmethod
    def _build_uri(meta: dict[str, Any], creds: dict[str, str]) -> str:
        """Compose a ``mongodb://`` URI from connection_meta + creds.

        Username and password are URL-encoded so passwords with ``@``
        or ``/`` in them don't poison the URI parser.
        """
        user = creds.get("user") or meta.get("user")
        password = creds.get("password") or meta.get("password")
        host = meta["host"]
        port = int(meta.get("port", 27017))
        db_name = meta["db_name"]

        auth_part = ""
        if user and password:
            auth_part = f"{quote_plus(str(user))}:{quote_plus(str(password))}@"

        params: list[str] = []
        params.append(
            f"authSource={quote_plus(str(meta.get('auth_source') or db_name))}"
        )
        if meta.get("tls"):
            params.append("tls=true")
        if meta.get("replica_set"):
            params.append(
                f"replicaSet={quote_plus(str(meta['replica_set']))}"
            )
        if meta.get("direct_connection"):
            params.append("directConnection=true")

        return f"mongodb://{auth_part}{host}:{port}/{db_name}?" + "&".join(params)

    async def aclose(self) -> None:
        # motor's close is synchronous; safe to call from async context.
        try:
            self._client.close()
        except Exception:  # pragma: no cover — best-effort
            pass

    # ── Schema introspection ────────────────────────────────────────

    async def introspect_schema(self) -> SchemaBundle:
        """List user collections in the configured database and infer
        a coarse "column" set by sampling up to 100 documents per
        collection.

        Mongo has no FKs and no DDL-level schema — we emit one
        :class:`TableMeta` per collection with ``foreign_keys=[]`` and
        ``columns`` derived from the union of top-level keys seen in
        the sample. ``_id`` is flagged as the primary key.
        """
        names = await self._db.list_collection_names()
        tables: list[TableMeta] = []
        for coll in sorted(names):
            if coll.startswith("system."):
                continue
            cursor = self._db[coll].find({}, limit=100)
            sample_docs: list[dict[str, Any]] = []
            async for doc in cursor:
                sample_docs.append(doc)
            cols = _infer_columns(sample_docs)
            if not cols:
                # Empty / unsampleable collection — still expose it as
                # a table so the planner knows it exists. ``_id`` is
                # universal.
                cols = [
                    ColumnMeta(
                        name="_id",
                        data_type="objectid",
                        nullable=False,
                        is_pk=True,
                    )
                ]
            tables.append(
                TableMeta(
                    schema=self._db_name,
                    name=coll,
                    columns=cols,
                    foreign_keys=[],
                )
            )
        return SchemaBundle(dialect=self.dialect, tables=tables, samples={})

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        """Distinct values via a cheap ``$group`` aggregation.

        For numeric-looking columns we also collect min/max/avg.
        Mongo's ``distinct()`` doesn't take a limit, so we run a
        bounded aggregation pipeline instead.
        """
        # _id columns and other IDs aren't useful as categorical samples.
        if col.is_id or col.is_pk or col.name == "_id":
            return ColumnSample()

        coll = self._db[table.name]
        try:
            cursor = coll.aggregate(
                [
                    {"$group": {"_id": f"${col.name}"}},
                    {"$limit": 51},
                ]
            )
            vals: list[Any] = []
            async for row in cursor:
                v = row.get("_id")
                if v is None:
                    continue
                vals.append(_coerce_doc_cell(v))
        except Exception:
            return ColumnSample()

        is_numeric = col.data_type in {"bigint", "double", "numeric", "integer", "float"}
        numeric_stats: dict[str, float] | None = None
        if is_numeric:
            try:
                cursor = coll.aggregate(
                    [
                        {
                            "$group": {
                                "_id": None,
                                "min": {"$min": f"${col.name}"},
                                "max": {"$max": f"${col.name}"},
                                "avg": {"$avg": f"${col.name}"},
                            }
                        }
                    ]
                )
                async for row in cursor:
                    stats: dict[str, float] = {}
                    for k in ("min", "max", "avg"):
                        v = row.get(k)
                        if isinstance(v, (int, float, Decimal)):
                            stats[k] = float(v)
                    if stats:
                        numeric_stats = stats
                    break
            except Exception:
                pass

        return ColumnSample(
            distinct_values=vals[:50] if vals else None,
            distinct_truncated=(len(vals) >= 51),
            numeric_stats=numeric_stats,
        )

    # ── Read-only validation ────────────────────────────────────────

    def validate_readonly(self, sql: str) -> ValidationResult:
        """Validate the JSON envelope. The Protocol calls this argument
        ``sql`` for SQL engines — for Mongo it's a JSON envelope string."""
        result, _ = validate_mongo_query(sql)
        return result

    # ── Execute ─────────────────────────────────────────────────────

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        """Run the aggregation pipeline. ``sql`` is the JSON envelope.

        Documents are flattened into rows by union-of-top-level-keys,
        preserving first-seen order. BSON-only types (ObjectId,
        Decimal128, datetime) are coerced to JSON-friendly Python
        scalars so the downstream LLM nodes don't choke.
        """
        result, envelope = validate_mongo_query(sql)
        if not result.ok or envelope is None:
            raise ValueError(
                "Refusing to execute: "
                + "; ".join(f.message for f in result.findings)
            )

        database = envelope["database"]
        collection = envelope["collection"]
        pipeline = envelope["pipeline"]

        # Defense in depth — if the validator's default $limit got
        # stripped somehow, enforce row_cap by appending one more.
        has_limit = any(
            isinstance(s, dict) and "$limit" in s for s in pipeline
        )
        if not has_limit:
            pipeline.append({"$limit": row_cap})

        db = (
            self._db if database == self._db_name else self._client[database]
        )
        coll = db[collection]

        t0 = time.perf_counter()
        cursor = coll.aggregate(
            pipeline,
            maxTimeMS=int(timeout_s * 1000),
        )

        docs: list[dict[str, Any]] = []
        async for doc in cursor:
            docs.append(doc)
            if len(docs) > row_cap:
                break
        took_ms = int((time.perf_counter() - t0) * 1000)

        # Union top-level keys preserving first-seen order.
        col_order: "OrderedDict[str, None]" = OrderedDict()
        for d in docs:
            for k in d.keys():
                if k not in col_order:
                    col_order[k] = None
        columns = list(col_order.keys())

        rows: list[list[Any]] = []
        for d in docs[:row_cap]:
            rows.append([_coerce_doc_cell(d.get(c)) for c in columns])

        truncated = len(docs) > row_cap
        return ResultSet(
            columns=columns,
            dtypes=["string"] * len(columns),  # Mongo is schemaless; best-effort
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            took_ms=took_ms,
        )


# ── helpers ────────────────────────────────────────────────────────


def _infer_columns(sample_docs: list[dict[str, Any]]) -> list[ColumnMeta]:
    """Union top-level keys across a document sample and label each
    with the dtype of its first non-null value.

    First-seen order is preserved so ``_id`` lands first.
    """
    cols: "OrderedDict[str, str]" = OrderedDict()
    for doc in sample_docs:
        for k, v in doc.items():
            if k in cols:
                if cols[k] == "null" and v is not None:
                    cols[k] = _python_type_name(v)
                continue
            cols[k] = _python_type_name(v)
    out: list[ColumnMeta] = []
    for name, dtype in cols.items():
        out.append(
            ColumnMeta(
                name=name,
                data_type=dtype,
                nullable=True,
                is_pk=(name == "_id"),
            )
        )
    return out


def _coerce_doc_cell(value: Any) -> Any:
    """Coerce a BSON / nested cell to a JSON-serialisable scalar.

    The downstream agent runs sample rows through Pydantic and SSE
    serialization, so we can't pass ObjectId / Decimal128 / nested
    dicts through as-is.
    """
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    # Nested structures and BSON-only types (ObjectId, Decimal128,
    # Binary, etc.) round-trip via JSON. The default=str fallback
    # catches anything the encoder can't handle natively.
    if isinstance(value, (list, tuple, dict)):
        try:
            return json.dumps(value, default=str)
        except TypeError:
            return str(value)
    return str(value)
