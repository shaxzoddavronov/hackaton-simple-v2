"""OpenAPI 3.x / Swagger 2.0 spec parser.

We deliberately implement this as a small, dependency-free walker over
the spec ``dict`` rather than pulling in ``openapi-spec-validator`` or
``openapi3-parser`` — QueryMind needs only what the planner can reason
about:

  * the **endpoint catalog** (one entry per GET path),
  * **query / path parameters** with coarse types,
  * the **response body's top-level fields** (one level deep — nested
    objects surface as ``object`` and aren't drilled into).

The output is a list of :class:`ParsedEndpoint` dicts that the
:mod:`app.engines.rest_api` adapter maps onto the same
``TableMeta``/``SchemaBundle`` shape the rest of the agent already
consumes. That keeps REST APIs a drop-in dialect — chart_designer and
answer_writer don't need to know they're talking to HTTP.

Only GET endpoints are surfaced — REST APIs in QueryMind are strictly
read-only, and the validator enforces ``method == "GET"`` separately.

``$ref`` resolution: local refs ``#/components/schemas/Foo`` (3.x) and
``#/definitions/Foo`` (2.0) are followed. External refs (``http://…``
or filesystem paths) are intentionally NOT supported — they're rare in
the wild and resolving them would require a network call at
introspection time which we want to avoid.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedParam:
    name: str
    location: str  # "query" | "path" | "header"
    type: str      # coarse: "string"|"integer"|"number"|"boolean"|"array"|"object"
    required: bool = False
    description: str = ""


@dataclass
class ParsedField:
    name: str
    type: str
    nullable: bool = True
    description: str = ""


@dataclass
class ParsedEndpoint:
    method: str       # always "GET" in our catalog
    path: str         # e.g. "/api/v1/contacts" or "/users/{id}"
    summary: str = ""
    description: str = ""
    operation_id: str | None = None
    params: list[ParsedParam] = field(default_factory=list)
    response_fields: list[ParsedField] = field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────


def load_spec_text(spec_text: str) -> dict[str, Any]:
    """Parse a spec string as JSON. YAML is intentionally unsupported in
    v1 — pass the spec through ``yq -o json`` upstream if you have YAML.
    """
    data = json.loads(spec_text)
    if not isinstance(data, dict):
        raise ValueError("OpenAPI spec root must be a JSON object")
    return data


def load_spec_base64(b64: str) -> dict[str, Any]:
    """Decode a base64-encoded JSON spec (the form the frontend uploads).

    The frontend reads the file via ``FileReader``, base64-encodes the
    bytes, and ships them inside ``connection_meta.spec_content_b64``.
    """
    raw = base64.b64decode(b64, validate=True).decode("utf-8")
    return load_spec_text(raw)


def parse_openapi(spec: dict[str, Any]) -> list[ParsedEndpoint]:
    """Walk an OpenAPI 3.x or Swagger 2.0 spec and yield one
    :class:`ParsedEndpoint` per ``GET`` operation.

    Non-GET methods, deprecated operations, and operations missing a
    path or response schema all yield endpoints with empty fields —
    they're still surfaced so the planner can see them, but they won't
    carry response columns.
    """
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return []

    endpoints: list[ParsedEndpoint] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        # Path-level parameters are shared by every method under the path.
        shared_params = _parse_params(item.get("parameters") or [], spec)
        for method_name, op in item.items():
            if method_name.lower() != "get":
                continue
            if not isinstance(op, dict):
                continue
            params = list(shared_params)
            params.extend(_parse_params(op.get("parameters") or [], spec))
            endpoints.append(
                ParsedEndpoint(
                    method="GET",
                    path=path,
                    summary=str(op.get("summary") or ""),
                    description=str(op.get("description") or ""),
                    operation_id=op.get("operationId"),
                    params=params,
                    response_fields=_parse_response_fields(op, spec),
                )
            )
    return endpoints


# ── Parameter parsing ─────────────────────────────────────────────


def _parse_params(
    raw_params: list[Any], spec: dict[str, Any]
) -> list[ParsedParam]:
    out: list[ParsedParam] = []
    for p in raw_params:
        if not isinstance(p, dict):
            continue
        p = _resolve_ref(p, spec)
        loc = str(p.get("in") or "")
        if loc not in {"query", "path", "header"}:
            # Skip cookie/body params — query is the only one our planner
            # uses extensively, and path is needed for templating.
            continue
        # OpenAPI 3 nests schema under ``schema``; Swagger 2 inlines type.
        schema = p.get("schema") if isinstance(p.get("schema"), dict) else p
        out.append(
            ParsedParam(
                name=str(p.get("name") or ""),
                location=loc,
                type=_coerce_type(schema),
                required=bool(p.get("required") or loc == "path"),
                description=str(p.get("description") or ""),
            )
        )
    return out


# ── Response parsing ──────────────────────────────────────────────


def _parse_response_fields(
    op: dict[str, Any], spec: dict[str, Any]
) -> list[ParsedField]:
    """Extract the response body's top-level fields.

    Strategy:
      1. Look at responses["200"] (or "201"/"2XX"/"default") in priority.
      2. For OpenAPI 3, dig through content["application/json"].schema.
         For Swagger 2, the schema is at responses["200"].schema directly.
      3. Resolve a single-level ``$ref`` if present.
      4. If schema is ``type=array``, drill into ``items``.
      5. Surface the schema's ``properties`` as ParsedField rows. Nested
         objects/arrays are surfaced as a single field with a coarse
         type — we don't recurse, the planner has no use for it.
    """
    responses = op.get("responses") or {}
    if not isinstance(responses, dict):
        return []

    # Pick the most successful response shape.
    pick_order = ("200", "201", "2XX", "default")
    response: dict[str, Any] | None = None
    for code in pick_order:
        candidate = responses.get(code)
        if isinstance(candidate, dict):
            response = candidate
            break
    if response is None:
        # Fall back to any 2xx response.
        for code, candidate in responses.items():
            if str(code).startswith("2") and isinstance(candidate, dict):
                response = candidate
                break
    if response is None:
        return []

    response = _resolve_ref(response, spec)

    # OpenAPI 3: content["application/json"].schema
    schema = None
    content = response.get("content")
    if isinstance(content, dict):
        for media_type in ("application/json", "*/*"):
            entry = content.get(media_type)
            if isinstance(entry, dict) and isinstance(entry.get("schema"), dict):
                schema = entry["schema"]
                break
        # Fallback: pick the first content entry with a schema.
        if schema is None:
            for entry in content.values():
                if isinstance(entry, dict) and isinstance(entry.get("schema"), dict):
                    schema = entry["schema"]
                    break

    # Swagger 2: schema sits directly on the response object.
    if schema is None and isinstance(response.get("schema"), dict):
        schema = response["schema"]

    if schema is None:
        return []

    schema = _resolve_ref(schema, spec)

    # Many list endpoints wrap rows in {"data": [...]} or return a bare
    # array. If the top-level is an array, drill in.
    if schema.get("type") == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            schema = _resolve_ref(items, spec)

    props = schema.get("properties")
    if not isinstance(props, dict):
        return []

    required = set(schema.get("required") or [])
    fields: list[ParsedField] = []
    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue
        prop = _resolve_ref(prop, spec)
        fields.append(
            ParsedField(
                name=str(name),
                type=_coerce_type(prop),
                nullable=name not in required,
                description=str(prop.get("description") or ""),
            )
        )
    return fields


# ── Type coercion ────────────────────────────────────────────────


def _coerce_type(schema: dict[str, Any] | None) -> str:
    """Bucket an OpenAPI schema type into a coarse dtype string used by
    the planner / UI."""
    if not isinstance(schema, dict):
        return "string"
    t = str(schema.get("type") or "").lower()
    fmt = str(schema.get("format") or "").lower()
    if t == "integer":
        return "integer"
    if t == "number":
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "array":
        return "array"
    if t == "object":
        return "object"
    if t == "string":
        if fmt in ("date", "date-time"):
            return "timestamp"
        if fmt == "uuid":
            return "uuid"
        return "string"
    # Type unspecified — best guess from format/enum/keywords.
    if "enum" in schema:
        return "string"
    return "string"


# ── $ref resolver ────────────────────────────────────────────────


def _resolve_ref(node: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Follow a single ``$ref`` within ``spec``. Local refs only.

    Returns ``node`` unchanged if there is no ``$ref`` or if the ref
    points outside the document. We deliberately don't recurse through
    chained refs (``A → B → C``) — one hop covers the vast majority of
    real-world specs and chained refs in the wild are rare enough that
    the planner can deal with the unresolved shape.
    """
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    parts = ref[2:].split("/")
    cur: Any = spec
    for p in parts:
        # JSON Pointer encodes "/" as "~1" and "~" as "~0".
        key = p.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return node
    if isinstance(cur, dict):
        return cur
    return node
