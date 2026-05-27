"""Elasticsearch ``QueryEngine`` adapter.

ES is a non-SQL backend, but it slots into the existing
:class:`QueryEngine` Protocol with two convenient lies:

  * The ``sql`` argument passed to :meth:`execute` and
    :meth:`validate_readonly` is a **JSON envelope string**:

        {"index": "logs-*", "body": {"query": ..., "aggs": ...}}

    The planner is taught to emit it; the validator parses it back.

  * Result handling produces a tabular :class:`ResultSet` so the rest
    of the agent (chart_designer, answer_writer) doesn't need to know
    we're talking to ES. Aggregation buckets become rows; plain hits
    flatten ``_source`` keys.

Security: every request goes through
:func:`app.services.es_readonly_validator.validate_es_query`. That
validator hard-rejects ``script``, ``_delete_by_query``, ``_reindex``,
and similar mutation / scripting surfaces. Hidden / dot-prefixed
system indices are off-limits too.

Connection metadata shape (``WorkspaceConnection.connection_meta``):

  * ``hosts``: list[str] or str — e.g. ``["http://es:9200"]``.
  * ``verify_certs``: bool, default true.
  * ``request_timeout``: int seconds, default 10.

Credentials (decrypted, attached as ``_credentials`` dict by callers):

  * ``api_key`` — single string OR ``"<id>:<api_key>"``.
  * OR ``user`` + ``password`` for HTTP basic auth.
"""
from __future__ import annotations

import json
import time
from typing import Any

from elasticsearch import AsyncElasticsearch

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
from app.services.es_readonly_validator import validate_es_query


# A type-mapping shorthand from ES field types to our generic dtype
# strings. Unknown types fall through to the raw ES name.
_ES_TO_DTYPE = {
    "keyword": "string",
    "text": "string",
    "wildcard": "string",
    "constant_keyword": "string",
    "ip": "string",
    "byte": "integer",
    "short": "integer",
    "integer": "integer",
    "long": "bigint",
    "unsigned_long": "bigint",
    "float": "float",
    "double": "double",
    "half_float": "float",
    "scaled_float": "float",
    "boolean": "boolean",
    "date": "timestamp",
    "date_nanos": "timestamp",
    "geo_point": "geo",
    "geo_shape": "geo",
    "object": "object",
    "nested": "nested",
}


@register("elasticsearch")
class ElasticsearchEngine:
    dialect: Dialect = "elasticsearch"

    def __init__(self, source) -> None:
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}

        hosts = meta.get("hosts")
        if isinstance(hosts, str):
            hosts = [hosts]
        if not hosts or not isinstance(hosts, list):
            raise ValueError(
                "Elasticsearch connection_meta must include 'hosts' "
                "(list of URLs or a single URL string)"
            )

        kwargs: dict[str, Any] = {
            "hosts": hosts,
            "verify_certs": bool(meta.get("verify_certs", True)),
            "request_timeout": int(meta.get("request_timeout", 10)),
        }

        api_key = creds.get("api_key") or meta.get("api_key")
        user = creds.get("user") or meta.get("user")
        password = creds.get("password") or meta.get("password")

        if api_key:
            # ES accepts either "id:api_key" or just "<encoded>".
            kwargs["api_key"] = api_key
        elif user and password:
            kwargs["basic_auth"] = (user, password)

        self._client = AsyncElasticsearch(**kwargs)

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:  # pragma: no cover — best-effort
            pass

    # ── Schema introspection ────────────────────────────────────────

    async def introspect_schema(self) -> SchemaBundle:
        """Treat each index as a 'table' and each mapping field as a
        'column'. ES doesn't have foreign keys; the FK list stays empty.

        Only user indices are surfaced — anything starting with a dot
        is system-level and silently dropped.
        """
        mappings = await self._client.indices.get_mapping(index="*", expand_wildcards="open")
        # ``mappings`` is a dict of {index: {"mappings": {"properties": {...}}}}.

        tables: list[TableMeta] = []
        for index_name, payload in mappings.body.items():
            if index_name.startswith("."):
                continue
            props = (
                payload.get("mappings", {}).get("properties", {}) or {}
            )
            cols = list(_flatten_properties(props))
            if not cols:
                continue
            tables.append(
                TableMeta(
                    schema="_all",
                    name=index_name,
                    columns=cols,
                    foreign_keys=[],
                )
            )

        return SchemaBundle(dialect="elasticsearch", tables=tables, samples={})

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        """Pull up to 12 distinct values via a ``terms`` aggregation.

        Cheap on keyword/text fields; skipped for object/nested types
        because they aren't aggregatable.
        """
        if col.data_type in {"object", "nested", "geo", "text"}:
            return ColumnSample()

        # ``text`` fields can't be aggregated without ``.keyword``, but
        # we don't know the subfield wiring at introspection time. Skip
        # to keep things safe.
        try:
            resp = await self._client.search(
                index=table.name,
                size=0,
                aggs={
                    "vals": {
                        "terms": {"field": col.name, "size": 12, "missing": "__null__"}
                    }
                },
                request_timeout=5,
            )
        except Exception:
            return ColumnSample()

        buckets = (
            resp.body.get("aggregations", {}).get("vals", {}).get("buckets", []) or []
        )
        if not buckets:
            return ColumnSample()
        values = [b["key"] for b in buckets if b.get("key") != "__null__"]
        return ColumnSample(distinct_values=values, distinct_truncated=len(buckets) >= 12)

    # ── Read-only validation ────────────────────────────────────────

    def validate_readonly(self, sql: str) -> ValidationResult:
        """Validate the JSON envelope. The Protocol calls this argument
        ``sql`` for SQL engines — for ES it's a JSON envelope string."""
        result, _ = validate_es_query(sql)
        return result

    # ── Execute ─────────────────────────────────────────────────────

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        """Run the search request. ``sql`` is the JSON envelope string.

        Aggregations and hits are both supported. We prefer aggregations
        if present (the planner uses ``size:0`` for pure aggs).
        """
        result, envelope = validate_es_query(sql)
        if not result.ok or envelope is None:
            raise ValueError(
                "Refusing to execute: " + "; ".join(f.message for f in result.findings)
            )

        index = envelope["index"]
        body = envelope["body"]
        # Defense in depth — never let row_cap be exceeded.
        if int(body.get("size", 0)) > row_cap:
            body["size"] = row_cap

        t0 = time.perf_counter()
        resp = await self._client.search(index=index, body=body, request_timeout=timeout_s)
        took_ms = int((time.perf_counter() - t0) * 1000)

        # Prefer aggregations when present.
        aggs = resp.body.get("aggregations")
        if aggs:
            columns, dtypes, rows = _flatten_aggregations(aggs)
            return ResultSet(
                columns=columns,
                dtypes=dtypes,
                rows=rows,
                row_count=len(rows),
                truncated=False,
                took_ms=took_ms,
            )

        # Otherwise tabulate hits.
        hits = (resp.body.get("hits") or {}).get("hits") or []
        rows: list[list[Any]] = []
        col_order: list[str] = []
        col_seen: set[str] = set()
        for h in hits:
            src = h.get("_source") or {}
            for k in src.keys():
                if k not in col_seen:
                    col_seen.add(k)
                    col_order.append(k)
        for h in hits:
            src = h.get("_source") or {}
            rows.append([src.get(c) for c in col_order])

        truncated = len(rows) >= row_cap
        return ResultSet(
            columns=col_order,
            dtypes=["string"] * len(col_order),  # ES is JSON; dtype is best-effort
            rows=rows[:row_cap],
            row_count=len(rows[:row_cap]),
            truncated=truncated,
            took_ms=took_ms,
        )


# ── helpers ────────────────────────────────────────────────────────


def _flatten_properties(
    props: dict[str, Any], prefix: str = ""
) -> list[ColumnMeta]:
    """Walk ES mapping ``properties`` recursively into flat ColumnMeta
    rows. Nested objects use dotted paths (``user.email``)."""
    out: list[ColumnMeta] = []
    for name, defn in props.items():
        full = f"{prefix}.{name}" if prefix else name
        defn = defn or {}
        nested = defn.get("properties")
        if isinstance(nested, dict) and nested:
            # Surface the parent as a structural column for visibility…
            out.append(
                ColumnMeta(
                    name=full,
                    data_type=defn.get("type", "object"),
                    nullable=True,
                )
            )
            out.extend(_flatten_properties(nested, prefix=full))
            continue
        es_type = defn.get("type", "object")
        dtype = _ES_TO_DTYPE.get(es_type, es_type)
        out.append(
            ColumnMeta(
                name=full,
                data_type=dtype,
                nullable=True,
            )
        )
    return out


def _flatten_aggregations(
    aggs: dict[str, Any],
) -> tuple[list[str], list[str], list[list[Any]]]:
    """Turn an aggs payload into a tabular shape.

    Handles the common single-level / two-level cases:
      * terms / date_histogram → rows of (key, doc_count, metrics…)
      * nested sub-agg → cartesian product of bucket keys
    """
    # Identify the first bucket-producing aggregation.
    bucket_agg_name = None
    bucket_agg_payload = None
    for agg_name, payload in aggs.items():
        if isinstance(payload, dict) and isinstance(payload.get("buckets"), list):
            bucket_agg_name = agg_name
            bucket_agg_payload = payload
            break

    if bucket_agg_name is None:
        # Just metric aggs at the top level.
        columns = list(aggs.keys())
        row = [_extract_metric(aggs[c]) for c in columns]
        dtypes = ["float"] * len(columns)
        return columns, dtypes, [row]

    buckets = bucket_agg_payload["buckets"]
    if not buckets:
        return ["key", "doc_count"], ["string", "bigint"], []

    # Determine columns: key, doc_count, plus any sub-agg keys.
    sub_keys: list[str] = []
    for b in buckets:
        for k in b.keys():
            if k in {"key", "key_as_string", "doc_count", "doc_count_error_upper_bound", "sum_other_doc_count"}:
                continue
            if k not in sub_keys and isinstance(b[k], dict):
                sub_keys.append(k)

    columns: list[str] = [bucket_agg_name, "doc_count", *sub_keys]
    rows: list[list[Any]] = []
    for b in buckets:
        key = b.get("key_as_string") or b.get("key")
        # ``doc_count`` is normally an int (the bucket's auto count),
        # but a planner that wrote a SUB-AGGREGATION named ``doc_count``
        # (e.g. ``aggs.doc_count.value_count``) overwrites the bucket
        # field with a dict like ``{"value": N}``. Run the same metric
        # unwrap we use for sub_keys so the table cell is always a
        # primitive — otherwise it leaks into BarSpec.data as
        # ``{"value": 0}`` and crashes the frontend with "Objects are
        # not valid as a React child".
        row = [key, _extract_metric(b.get("doc_count"))]
        for sk in sub_keys:
            sub = b.get(sk)
            row.append(_extract_metric(sub))
        rows.append(row)
    # date_histogram buckets carry a ``key_as_string`` for every entry
    # AND a numeric epoch ``key``. Detect that shape so chart_designer
    # picks a line chart instead of degrading to a table just because
    # the dtype defaulted to "string".
    key_dtype = (
        "timestamp"
        if buckets
        and all(
            isinstance(b.get("key_as_string"), str)
            and _looks_like_iso_date(b["key_as_string"])
            for b in buckets
        )
        else "string"
    )
    dtypes = [key_dtype, "bigint"] + ["float"] * len(sub_keys)
    return columns, dtypes, rows


def _extract_metric(node: Any) -> Any:
    """Pull a scalar out of a metric agg payload."""
    if not isinstance(node, dict):
        return node
    for k in ("value", "values", "doc_count"):
        if k in node:
            return node[k]
    return node


_ISO_DATE_RE = __import__("re").compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


def _looks_like_iso_date(s: str) -> bool:
    """Heuristic — accept dates / datetimes that ES emits via
    ``key_as_string`` (e.g. ``"2026-04-01T00:00:00.000Z"`` or
    ``"2026-04-01"``)."""
    return bool(_ISO_DATE_RE.match(s))
