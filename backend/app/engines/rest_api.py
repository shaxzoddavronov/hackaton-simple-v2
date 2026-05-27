"""REST API engine adapter.

This is the 10th dialect, sitting alongside the 9 database adapters
(postgres/sqlite/mysql/clickhouse/oracle/mongodb/elasticsearch/duckdb/
mssql). Unlike the database adapters it speaks HTTP — but exposes the
same :class:`QueryEngine` Protocol so the rest of the agent pipeline
treats it identically.

The "query language" is a JSON envelope (mirroring the ES/Mongo
pattern):

    {
      "endpoint":        "/api/v1/contacts",
      "method":          "GET",
      "path_params":     {"id": "123"},
      "query_params":    {"limit": 100},
      "headers":         {},
      "json_path":       "$.data.items",
      "row_field_paths": {"id": "$.id", "name": "$.props.name"}
    }

Schema bundles for REST APIs come from one of three sources, in
priority order (driven by ``connection_meta.spec_source``):

  1. ``preset`` — a hard-coded catalog from
     :mod:`app.services.api_presets` (Bitrix24, AmoCRM, 1C OData,
     HubSpot, Salesforce).
  2. ``openapi_url`` — the engine fetches the spec at introspection
     time and parses it via :func:`openapi_parser.parse_openapi`.
  3. ``openapi_file`` — the user uploaded the spec; we decode the
     base64 content from connection_meta and parse it.
  4. ``none`` — no catalog; the agent can only query endpoints the
     user types manually (the validator's endpoint-allowlist check is
     skipped when the bundle is empty).

Auth (``auth_kind`` on the WorkspaceConnection):

  * ``bearer``        — ``Authorization: Bearer <token>``.
  * ``api_key``       — header OR query param, depending on
    ``credentials.key_location``.
  * ``basic``         — HTTP Basic from user/password.
  * ``oauth2_client`` — client_credentials grant; the engine fetches a
    token from ``token_url`` and caches it in memory. Refreshed on
    401 inside :meth:`execute`.
  * ``none``          — public API, no credentials.

Read-only is enforced THREE ways for the REST dialect, mirroring the
SQL/ES/Mongo pattern:

  1. **Parse layer** — :func:`api_query_validator.validate_api_query`
     hard-rejects ``method != "GET"`` and absolute URLs (SSRF guard).
  2. **Engine layer** — :meth:`execute` re-validates the envelope
     before the wire call (defence in depth).
  3. **Wire layer** — every outbound HTTP request is built with
     ``method="GET"`` regardless of what the envelope says, so even a
     bypassed validator can't issue a write.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
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
    ValidationResult,
)
from app.engines.registry import register
from app.services.api_presets import load_preset
from app.services.api_query_validator import validate_api_query
from app.services.openapi_parser import (
    ParsedEndpoint,
    load_spec_base64,
    load_spec_text,
    parse_openapi,
)

log = logging.getLogger(__name__)


class RestApiError(RuntimeError):
    """Raised when an outbound HTTP call returns a non-2xx response or
    the OAuth token-grant step fails. Carries ``status_code`` so callers
    can route on it (the federated executor surfaces it as a sub-query
    error)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


# Fallback JSON-path probes used when the planner doesn't supply
# ``json_path``. Order matters — the first non-empty array wins.
_DEFAULT_JSON_PATHS = (
    "$.data.items",
    "$.data.results",
    "$.data",
    "$.items",
    "$.results",
    "$.records",
    "$.value",  # OData
)


@register("rest_api")
class RestApiEngine:
    dialect: Dialect = "rest_api"

    def __init__(self, source) -> None:
        meta = dict(source.connection_meta or {})
        creds = getattr(source, "_credentials", None) or {}

        base_url = str(meta.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError(
                "REST API connection_meta must include 'base_url'"
            )

        self._base_url = base_url
        self._spec_source = str(meta.get("spec_source") or "none")
        self._spec_url = meta.get("spec_url")
        self._spec_content_b64 = meta.get("spec_content_b64")
        self._preset = meta.get("preset")
        self._default_headers: dict[str, str] = dict(
            meta.get("default_headers") or {}
        )
        self._timeout_s = int(meta.get("timeout_s") or 30)

        self._auth_kind = getattr(source, "auth_kind", None) or "none"
        self._credentials = creds

        # Cache for OAuth2 client_credentials tokens. Re-fetched lazily
        # on 401. We store ``(token, fetched_at)`` so future versions
        # can honour ``expires_in`` if needed; v1 just refreshes on 401.
        self._oauth_token: str | None = None
        self._client: httpx.AsyncClient | None = None

    def _http_client(self) -> httpx.AsyncClient:
        # Lazy single-instance client per engine so HTTP connection
        # reuse works across calls. Closed in :meth:`aclose`.
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_s),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Introspection ─────────────────────────────────────────────

    async def introspect_schema(self) -> SchemaBundle:
        """Build a :class:`SchemaBundle` from the configured spec source.

        Each endpoint becomes a ``TableMeta`` with ``name = "GET <path>"``
        and columns drawn from the response body's top-level fields
        plus the endpoint's query/path params (prefixed ``param:`` in
        the column ``data_type`` so the planner can tell them apart).
        """
        endpoints = await self._load_catalog()
        tables: list[TableMeta] = []
        for ep in endpoints:
            cols: list[ColumnMeta] = []
            # Response fields → ordinary columns.
            for f in ep.response_fields:
                cols.append(
                    ColumnMeta(
                        name=f.name,
                        data_type=f.type,
                        nullable=f.nullable,
                    )
                )
            # Query/path params → columns tagged with a ``param:`` prefix
            # so chart_designer/answer_writer can ignore them while the
            # planner still sees what's available.
            for p in ep.params:
                cols.append(
                    ColumnMeta(
                        name=f"@{p.name}",
                        data_type=f"param:{p.type}",
                        nullable=not p.required,
                    )
                )
            tables.append(
                TableMeta(
                    schema="api",
                    name=f"GET {ep.path}",
                    columns=cols,
                    foreign_keys=[],
                )
            )
        return SchemaBundle(dialect="rest_api", tables=tables)

    async def _load_catalog(self) -> list[ParsedEndpoint]:
        if self._spec_source == "preset":
            if not self._preset:
                raise ValueError(
                    "spec_source='preset' requires 'preset' in connection_meta"
                )
            return load_preset(str(self._preset))

        if self._spec_source == "openapi_file":
            if not self._spec_content_b64:
                raise ValueError(
                    "spec_source='openapi_file' requires "
                    "'spec_content_b64' in connection_meta"
                )
            spec = load_spec_base64(str(self._spec_content_b64))
            return parse_openapi(spec)

        if self._spec_source == "openapi_url":
            if not self._spec_url:
                raise ValueError(
                    "spec_source='openapi_url' requires 'spec_url' "
                    "in connection_meta"
                )
            client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s))
            try:
                resp = await client.get(str(self._spec_url))
                if resp.status_code >= 400:
                    raise RestApiError(
                        resp.status_code,
                        f"failed to fetch OpenAPI spec: HTTP {resp.status_code}",
                    )
                spec = load_spec_text(resp.text)
            finally:
                await client.aclose()
            return parse_openapi(spec)

        # spec_source == "none" → empty catalog; user supplies endpoints
        # explicitly through the chat. The validator will still allow
        # any endpoint because the catalog check is bundle-driven.
        return []

    async def sample_column(
        self, table: TableMeta, col: ColumnMeta
    ) -> ColumnSample:
        # Sampling an API column would burn user-quota and rate-limits.
        # Skip — the schema's response_fields already describe the shape.
        return ColumnSample()

    # ── Validation ────────────────────────────────────────────────

    def validate_readonly(self, sql: str) -> ValidationResult:
        result, _ = validate_api_query(sql)
        return result

    # ── Execute ──────────────────────────────────────────────────

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet:
        # Re-validate (defence in depth: the validator node already ran
        # but `execute` may be called directly from tests or future
        # call sites).
        result, env = validate_api_query(sql)
        if not result.ok or env is None:
            raise ValueError(
                "REST API envelope failed validation: "
                + "; ".join(f.message for f in result.findings[:3])
            )

        path = self._expand_path(env["endpoint"], env.get("path_params") or {})
        query_params = self._flatten_query_params(
            env.get("query_params") or {}
        )
        extra_headers = dict(self._default_headers)
        extra_headers.update(env.get("headers") or {})

        # Auth injection. Token-bearing schemes prepare a header (or
        # mutate query_params for `api_key` location=query); HTTP basic
        # uses httpx's `auth=` tuple.
        basic_auth: tuple[str, str] | None = None
        await self._inject_auth(extra_headers, query_params)
        if self._auth_kind == "basic":
            user = str(self._credentials.get("username") or "")
            password = str(self._credentials.get("password") or "")
            basic_auth = (user, password)

        # Hard-code the wire method to GET regardless of envelope —
        # belt + suspenders. The validator already enforced this; if
        # we somehow get past it the runtime won't do a write.
        client = self._http_client()
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                client.request(
                    "GET",
                    path,
                    params=query_params,
                    headers=extra_headers,
                    auth=basic_auth,
                ),
                timeout=timeout_s + 2,
            )
        except httpx.HTTPError as e:
            raise RestApiError(0, f"transport error: {e}") from e
        took_ms = int((time.perf_counter() - t0) * 1000)

        # Retry once on 401 if we're using OAuth — token may have expired.
        if resp.status_code == 401 and self._auth_kind == "oauth2_client":
            self._oauth_token = None
            await self._inject_auth(extra_headers, query_params)
            t1 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    client.request(
                        "GET",
                        path,
                        params=query_params,
                        headers=extra_headers,
                    ),
                    timeout=timeout_s + 2,
                )
            except httpx.HTTPError as e:
                raise RestApiError(0, f"transport error on retry: {e}") from e
            took_ms = int((time.perf_counter() - t1) * 1000)

        if resp.status_code >= 400:
            # Truncate the body to keep error messages from blowing up
            # the SSE frame; sanitization happens at the SSE layer too.
            snippet = resp.text[:500] if resp.text else ""
            raise RestApiError(
                resp.status_code,
                f"HTTP {resp.status_code}: {snippet}",
            )

        try:
            payload = resp.json()
        except ValueError as e:
            # If the response isn't JSON, surface the body as a single
            # 1-row, 1-column ResultSet — the user can still inspect it
            # via the answer node.
            return ResultSet(
                columns=["raw_body"],
                dtypes=["text"],
                rows=[[resp.text[:10_000]]],
                row_count=1,
                truncated=False,
                took_ms=took_ms,
            )

        rows_payload = _extract_rows(payload, env.get("json_path"))
        if not rows_payload:
            return ResultSet(
                columns=[], dtypes=[], rows=[], row_count=0,
                truncated=False, took_ms=took_ms,
            )

        row_paths_spec = env.get("row_field_paths") or {}
        columns, dtypes, rows = _flatten_rows(
            rows_payload, row_paths_spec, row_cap=row_cap
        )
        truncated = len(rows_payload) > row_cap

        return ResultSet(
            columns=columns,
            dtypes=dtypes,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            took_ms=took_ms,
        )

    # ── Auth ─────────────────────────────────────────────────────

    async def _inject_auth(
        self,
        headers: dict[str, str],
        query_params: list[tuple[str, str]],
    ) -> None:
        """Mutate ``headers``/``query_params`` to add auth in-place."""
        kind = self._auth_kind
        if kind == "bearer":
            token = str(self._credentials.get("token") or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return
        if kind == "api_key":
            key = str(self._credentials.get("key") or "")
            if not key:
                return
            location = str(self._credentials.get("key_location") or "header")
            key_name = str(self._credentials.get("key_name") or "X-API-Key")
            if location == "query":
                query_params.append((key_name, key))
            else:
                headers[key_name] = key
            return
        if kind == "oauth2_client":
            token = await self._oauth_get_or_refresh()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return
        # "basic" is handled by httpx auth= tuple at the call site.
        # "none" → no-op.

    async def _oauth_get_or_refresh(self) -> str | None:
        if self._oauth_token is not None:
            return self._oauth_token
        token_url = str(self._credentials.get("token_url") or "")
        client_id = str(self._credentials.get("client_id") or "")
        client_secret = str(self._credentials.get("client_secret") or "")
        scope = str(self._credentials.get("scope") or "")
        if not (token_url and client_id and client_secret):
            return None
        # Use a SEPARATE httpx client because the engine's main client
        # is bound to base_url and the token endpoint may be on a
        # different host (Salesforce's login.salesforce.com vs the
        # tenant endpoint, etc).
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s)) as c:
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
            if scope:
                data["scope"] = scope
            resp = await c.post(token_url, data=data)
        if resp.status_code >= 400:
            raise RestApiError(
                resp.status_code,
                f"OAuth token endpoint returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}",
            )
        try:
            tok = resp.json().get("access_token")
        except ValueError as e:
            raise RestApiError(
                resp.status_code,
                f"OAuth token endpoint returned non-JSON body: {e}",
            ) from e
        if not isinstance(tok, str):
            raise RestApiError(
                resp.status_code,
                "OAuth response missing 'access_token' string",
            )
        self._oauth_token = tok
        return tok

    # ── helpers ─────────────────────────────────────────────────

    def _expand_path(self, path: str, path_params: dict[str, Any]) -> str:
        """Substitute ``{name}`` segments using ``path_params``.

        Unsubstituted templates are left as-is (the upstream API will
        return a 404 — which we surface via :class:`RestApiError`).
        """
        out = path
        for name, value in path_params.items():
            out = out.replace("{" + str(name) + "}", str(value))
        return out

    def _flatten_query_params(
        self, qp: dict[str, Any]
    ) -> list[tuple[str, str]]:
        """Turn the envelope's ``query_params`` dict into a list of
        ``(key, value)`` tuples so list-valued params can be sent as
        repeated query keys (``?tag=a&tag=b``)."""
        out: list[tuple[str, str]] = []
        for k, v in qp.items():
            if isinstance(v, list):
                for item in v:
                    out.append((k, "" if item is None else str(item)))
            else:
                out.append((k, "" if v is None else str(v)))
        return out


# ── module-level row extractors ────────────────────────────────────


def _extract_rows(payload: Any, json_path: str | None) -> list[Any]:
    """Pull the row array out of an API response.

    If ``json_path`` is provided we resolve it; otherwise we probe
    common locations in :data:`_DEFAULT_JSON_PATHS`. Falling back
    further still: if the root is already a list, return it; if it's
    a dict containing a single list-valued key, return that.
    """
    if isinstance(json_path, str) and json_path:
        node = _resolve_dot_path(payload, json_path)
        if isinstance(node, list):
            return node
        if isinstance(node, dict):
            return [node]
        return []

    for jp in _DEFAULT_JSON_PATHS:
        node = _resolve_dot_path(payload, jp)
        if isinstance(node, list) and node:
            return node

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Single-list shape: {"contacts": [...]}.
        list_values = [v for v in payload.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return list_values[0]
        return [payload]
    return []


def _resolve_dot_path(node: Any, path: str) -> Any:
    """Resolve a tiny JSON-path subset: ``$.foo.bar`` or ``$.foo[0].bar``.

    Not a full jsonpath — we just need dot-walking + bracket-indexing
    because that's all the planner ever emits. Returns ``None`` if any
    step misses.
    """
    if path.startswith("$"):
        path = path[1:]
    path = path.lstrip(".")
    if not path:
        return node
    cur: Any = node
    # Split on '.' but keep bracket indices attached to the previous
    # segment: "items[0].name" → ["items[0]", "name"].
    for seg in path.split("."):
        idx_start = seg.find("[")
        if idx_start >= 0:
            key = seg[:idx_start]
            if key:
                if not isinstance(cur, dict) or key not in cur:
                    return None
                cur = cur[key]
            remainder = seg[idx_start:]
            while remainder.startswith("[") and "]" in remainder:
                end = remainder.index("]")
                inside = remainder[1:end]
                try:
                    i = int(inside)
                except ValueError:
                    return None
                if not isinstance(cur, list) or not (0 <= i < len(cur)):
                    return None
                cur = cur[i]
                remainder = remainder[end + 1 :]
        else:
            if not isinstance(cur, dict) or seg not in cur:
                return None
            cur = cur[seg]
    return cur


def _flatten_rows(
    rows: list[Any],
    row_field_paths: dict[str, str],
    *,
    row_cap: int,
) -> tuple[list[str], list[str], list[list[Any]]]:
    """Turn a list of dict rows into columnar form.

    Two modes:
      * ``row_field_paths`` provided — keys become column names and
        the corresponding dot-paths are evaluated against each row.
      * Empty — use the union of top-level keys from the first 20
        sampled rows, skipping nested objects/arrays so the table
        stays scalar.
    """
    capped = rows[:row_cap]

    if row_field_paths:
        columns = list(row_field_paths.keys())
        out_rows: list[list[Any]] = []
        for r in capped:
            row: list[Any] = []
            for col in columns:
                val = _resolve_dot_path(r, row_field_paths[col])
                row.append(val)
            out_rows.append(row)
        dtypes = _infer_dtypes(out_rows, columns)
        return columns, dtypes, out_rows

    # Auto-flatten — pick top-level scalar keys.
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
        # All rows are non-dict (e.g. list of strings). Single-column.
        out_rows = [[r] for r in capped]
        return ["value"], _infer_dtypes(out_rows, ["value"]), out_rows
    out_rows = []
    for r in capped:
        if not isinstance(r, dict):
            out_rows.append([None] * len(seen))
            continue
        out_rows.append([r.get(c) for c in seen])
    dtypes = _infer_dtypes(out_rows, seen)
    return seen, dtypes, out_rows


def _infer_dtypes(rows: list[list[Any]], columns: list[str]) -> list[str]:
    """Look at the first non-null value of each column and bucket it
    into one of the dtype strings the UI knows about."""
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
