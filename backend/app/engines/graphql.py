"""GraphQL connector (Phase 32).

QueryMind's REST API engine (Phase 12) covers HTTP endpoints with
OpenAPI / OData / vendor-preset shapes. GraphQL APIs are
different enough — single endpoint, typed query language,
introspection-based schema — that they earn their own adapter.

Targets: Shopify Storefront API, GitHub v4, Hasura, Saleor,
Linear, Hashnode — any modern SaaS that exposes GraphQL.

Read-only enforcement:

  1. **Parse** — :func:`validate_readonly` parses the GraphQL
     document via ``graphql-core``. Operation type MUST be
     ``query``; ``mutation`` and ``subscription`` are rejected.
     Multi-operation documents must have every operation typed
     ``query``.

  2. **Wire** — every outbound HTTP request uses POST with the
     standard ``{query, variables}`` body. We never set the
     ``operationName`` to a mutation alias; the parsed document
     drives what gets sent.

Connection meta::

    {
      "endpoint": "https://api.github.com/graphql",
      "default_headers": {"Accept": "application/json"},
      "timeout_s": 30
    }

Credentials (same auth_kind union as REST API, Phase 12):

    bearer:       {"token": "..."}
    api_key:      {"key": "...", "key_location": "header|query",
                   "key_name": "X-API-Key"}
    basic:        {"username": "...", "password": "..."}
    none:         {}
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from app.engines.base import (
    ColumnMeta,
    ColumnSample,
    Dialect,
    ResultSet,
    SchemaBundle,
    TableMeta,
    ValidationFinding,
    ValidationResult,
)
from app.engines.registry import register


# Standard GraphQL introspection query — restricted to root Query
# fields + their immediate type. Production schemas are huge; we
# don't recurse beyond two levels.
_INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType {
      name
      fields {
        name
        description
        args { name type { name kind ofType { name kind } } }
        type {
          name
          kind
          ofType {
            name
            kind
            ofType {
              name
              kind
              ofType { name kind }
            }
          }
        }
      }
    }
    types {
      name
      kind
      fields { name type { name kind ofType { name kind } } }
    }
  }
}
""".strip()


@register("graphql")
class GraphqlEngine:
    dialect: Dialect = "graphql"

    def __init__(self, source) -> None:
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}
        endpoint = str(meta.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise ValueError(
                "GraphQL connection_meta missing 'endpoint'"
            )
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(
                "GraphQL 'endpoint' must be a full http(s) URL"
            )
        self._endpoint = endpoint
        self._default_headers: dict[str, str] = dict(
            meta.get("default_headers") or {}
        )
        self._timeout_s = int(meta.get("timeout_s") or 30)
        self._auth_kind = getattr(source, "auth_kind", None) or "none"
        self._credentials = creds

    # ── Introspection ─────────────────────────────────────────────

    async def introspect_schema(self) -> SchemaBundle:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s)
        ) as client:
            headers = dict(self._default_headers)
            query_params: list[tuple[str, str]] = []
            self._inject_auth(headers, query_params)
            resp = await client.post(
                self._endpoint,
                json={"query": _INTROSPECTION_QUERY},
                headers=headers,
                params=query_params or None,
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GraphQL introspection failed: HTTP {resp.status_code} — "
                f"{resp.text[:300]}"
            )
        data = resp.json().get("data") or {}
        schema_obj = data.get("__schema") or {}
        query_type = schema_obj.get("queryType") or {}
        root_fields = query_type.get("fields") or []
        types_index = {
            t["name"]: t for t in (schema_obj.get("types") or []) if t.get("name")
        }

        # Each root Query field is one "table". The columns are the
        # underlying object type's fields. Scalars / lists-of-scalars
        # at the root surface as 0-column tables (still useful — the
        # planner sees them and can call them).
        tables: list[TableMeta] = []
        for field in root_fields:
            tname = str(field.get("name") or "")
            if not tname or tname.startswith("__"):
                continue
            inner_type_name = _unwrap_type_name(field.get("type") or {})
            cols: list[ColumnMeta] = []
            if inner_type_name and inner_type_name in types_index:
                inner = types_index[inner_type_name]
                for sub in inner.get("fields") or []:
                    sub_name = str(sub.get("name") or "")
                    if not sub_name or sub_name.startswith("__"):
                        continue
                    sub_type = _unwrap_type_name(sub.get("type") or {}) or "Unknown"
                    cols.append(
                        ColumnMeta(
                            name=sub_name,
                            data_type=sub_type,
                            nullable=True,
                        )
                    )
            tables.append(
                TableMeta(
                    schema="query",
                    name=tname,
                    columns=cols,
                    foreign_keys=[],
                )
            )
        return SchemaBundle(dialect="graphql", tables=tables)

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        # Sampling against a GraphQL endpoint burns API quota.
        # Skip — the schema's named types describe the shape.
        return ColumnSample()

    # ── Validation ────────────────────────────────────────────────

    def validate_readonly(self, sql: str) -> ValidationResult:
        """Parse the GraphQL document and verify every operation is
        a query.

        ``sql`` is misnamed at the protocol level — the
        QueryEngine surface calls it that, but for GraphQL we pass
        a JSON envelope ``{"query": "...", "variables": {...}}``
        because the planner uses one consistent serialised shape
        across all non-SQL dialects.
        """
        try:
            envelope = json.loads(sql)
        except (ValueError, TypeError) as e:
            return ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_invalid_envelope",
                        message=f"envelope is not valid JSON: {e}",
                    )
                ],
            )
        if not isinstance(envelope, dict):
            return ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_invalid_envelope",
                        message="envelope must be a JSON object",
                    )
                ],
            )
        query = envelope.get("query")
        if not isinstance(query, str) or not query.strip():
            return ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_missing_query",
                        message="envelope.query is required",
                    )
                ],
            )

        # Parse via graphql-core — local import keeps the wheel
        # off the import path for installs that don't use GraphQL.
        try:
            from graphql import parse as gql_parse
            from graphql.language.ast import OperationDefinitionNode
        except ImportError:
            # Fallback: keyword check. Less robust but lets the
            # engine work in dev envs without graphql-core.
            lower = query.lower()
            if "mutation" in lower or "subscription" in lower:
                return ValidationResult(
                    ok=False,
                    findings=[
                        ValidationFinding(
                            code="graphql_write_operation",
                            message=(
                                "mutation / subscription rejected; "
                                "install graphql-core for proper AST validation"
                            ),
                        )
                    ],
                )
            return ValidationResult(ok=True, rewritten_sql=sql)

        try:
            ast = gql_parse(query)
        except Exception as e:
            return ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_parse_error",
                        message=f"failed to parse: {e}",
                    )
                ],
            )

        findings: list[ValidationFinding] = []
        for defn in ast.definitions:
            if isinstance(defn, OperationDefinitionNode):
                op = (defn.operation.value or "").lower()
                if op != "query":
                    findings.append(
                        ValidationFinding(
                            code="graphql_write_operation",
                            message=(
                                f"operation '{op}' is not allowed; only "
                                "'query' is read-only"
                            ),
                        )
                    )
        if findings:
            return ValidationResult(ok=False, findings=findings)
        return ValidationResult(ok=True, rewritten_sql=sql)

    # ── Execute ──────────────────────────────────────────────────

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        result = self.validate_readonly(sql)
        if not result.ok:
            raise ValueError(
                f"read-only validation failed: "
                f"{[f.code for f in result.findings]}"
            )
        envelope = json.loads(sql)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s + 2)
        ) as client:
            headers = dict(self._default_headers)
            query_params: list[tuple[str, str]] = []
            self._inject_auth(headers, query_params)
            t0 = time.perf_counter()
            resp = await asyncio.wait_for(
                client.post(
                    self._endpoint,
                    json={
                        "query": envelope["query"],
                        "variables": envelope.get("variables") or {},
                    },
                    headers=headers,
                    params=query_params or None,
                ),
                timeout=timeout_s + 2,
            )
        took_ms = int((time.perf_counter() - t0) * 1000)

        if resp.status_code >= 400:
            raise RuntimeError(
                f"GraphQL HTTP {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json()
        if body.get("errors"):
            # GraphQL surfaces query errors in the body with HTTP 200.
            err_msg = "; ".join(
                str(e.get("message", e))
                for e in body["errors"]
                if isinstance(e, dict)
            )
            raise RuntimeError(f"GraphQL errors: {err_msg[:300]}")
        data = body.get("data") or {}
        # Pick the first top-level field's value as the row payload.
        # GraphQL queries can return multiple top-level fields; we
        # surface the largest array, falling back to wrapping the
        # whole data dict in a single row.
        rows_data = _extract_rows_from_graphql(data)
        columns, dtypes, rows = _flatten_graphql_rows(rows_data, row_cap)
        truncated = len(rows_data) > row_cap
        return ResultSet(
            columns=columns,
            dtypes=dtypes,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            took_ms=took_ms,
        )

    async def aclose(self) -> None:
        return None

    # ── helpers ─────────────────────────────────────────────────

    def _inject_auth(
        self,
        headers: dict[str, str],
        query_params: list[tuple[str, str]],
    ) -> None:
        kind = self._auth_kind
        if kind == "bearer":
            token = str(self._credentials.get("token") or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif kind == "api_key":
            key = str(self._credentials.get("key") or "")
            if not key:
                return
            location = str(
                self._credentials.get("key_location") or "header"
            )
            key_name = str(
                self._credentials.get("key_name") or "X-API-Key"
            )
            if location == "query":
                query_params.append((key_name, key))
            else:
                headers[key_name] = key
        elif kind == "basic":
            import base64

            user = str(self._credentials.get("username") or "")
            pw = str(self._credentials.get("password") or "")
            token = base64.b64encode(f"{user}:{pw}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"


def _unwrap_type_name(type_ref: dict) -> str | None:
    """Drill through NON_NULL / LIST wrappers and return the
    underlying named type."""
    cur = type_ref
    for _ in range(8):
        if not isinstance(cur, dict):
            return None
        if cur.get("name"):
            return str(cur["name"])
        cur = cur.get("ofType") or {}
    return None


def _extract_rows_from_graphql(data: dict) -> list:
    """Pick the most-likely "rows" payload out of a GraphQL data
    dict. Strategy:
      1. If any top-level field is a list, return its longest one.
      2. Else if the single top-level field is a dict with an
         ``edges`` array (Relay-style connection), return that
         array's ``node`` values.
      3. Else wrap the whole data dict in a one-element list.
    """
    if not isinstance(data, dict) or not data:
        return []
    list_values: list[tuple[str, list]] = [
        (k, v) for k, v in data.items() if isinstance(v, list)
    ]
    if list_values:
        list_values.sort(key=lambda kv: len(kv[1]), reverse=True)
        return list_values[0][1]
    # Relay-style connection?
    for v in data.values():
        if isinstance(v, dict):
            edges = v.get("edges")
            if isinstance(edges, list):
                return [
                    e.get("node")
                    for e in edges
                    if isinstance(e, dict)
                ]
    return [data]


def _flatten_graphql_rows(
    rows_data: list, row_cap: int
) -> tuple[list[str], list[str], list[list[Any]]]:
    """Turn a list of GraphQL nodes (dicts) into columnar form.
    Same shape as the REST API engine's row flattener — keeps the
    UI consistent across both."""
    capped = rows_data[:row_cap]
    if not capped:
        return [], [], []
    if not isinstance(capped[0], dict):
        return ["value"], ["text"], [[r] for r in capped]
    seen: list[str] = []
    seen_set: set[str] = set()
    for sample in capped[:20]:
        if not isinstance(sample, dict):
            continue
        for k, v in sample.items():
            if k in seen_set:
                continue
            if isinstance(v, (dict, list)):
                continue
            seen.append(k)
            seen_set.add(k)
    if not seen:
        return ["value"], ["text"], [
            [str(r)] for r in capped
        ]
    rows: list[list[Any]] = []
    for r in capped:
        if not isinstance(r, dict):
            rows.append([None] * len(seen))
            continue
        rows.append([r.get(c) for c in seen])
    dtypes = _infer_dtypes(rows, seen)
    return seen, dtypes, rows


def _infer_dtypes(
    rows: list[list[Any]], columns: list[str]
) -> list[str]:
    out: list[str] = []
    for i in range(len(columns)):
        bucket = "unknown"
        for r in rows:
            if i >= len(r):
                continue
            v = r[i]
            if v is None:
                continue
            if isinstance(v, bool):
                bucket = "boolean"
            elif isinstance(v, int):
                bucket = "bigint"
            elif isinstance(v, float):
                bucket = "double"
            elif isinstance(v, str):
                bucket = "text"
            else:
                bucket = "text"
            break
        out.append(bucket)
    return out
