"""Read-only validator for the REST-API dialect.

The planner emits a single JSON envelope into ``SqlPlan.sql`` when the
target connection has ``dialect="rest_api"``. The shape is::

    {
      "endpoint":     "/api/v1/contacts",
      "method":       "GET",
      "path_params":  {"id": "123"},
      "query_params": {"limit": 100, "offset": 0},
      "headers":      {},
      "json_path":    "$.data.items",
      "row_field_paths": {"id": "$.id", "name": "$.props.name"}
    }

Validator responsibilities (mirrors the ES / Mongo validators in
shape — security boundary BEFORE the engine touches the network):

  * Parse the envelope (reject malformed JSON / wrong root type).
  * Require ``method == "GET"`` — POST/PUT/PATCH/DELETE are write
    actions, so REST APIs are read-only by construction.
  * Reject endpoints that contain path traversal (``..``).
  * Reject endpoint strings that look like absolute URLs (``http://``
    / ``https://``) — the engine prepends ``base_url`` itself; an
    absolute URL in the planner output is SSRF bait.
  * If a SchemaBundle is supplied, the endpoint MUST match one of the
    catalogued paths. Path templates with ``{param}`` segments are
    matched segment-by-segment so concrete IDs like ``/users/123``
    align with their template ``/users/{id}``.
  * Type-check path_params / query_params / headers (flat dict of
    scalars or list of scalars) and json_path / row_field_paths.

Returns ``(ValidationResult, parsed_envelope_or_None)`` so the engine
can reuse the cleaned envelope without re-parsing.
"""
from __future__ import annotations

import json
from typing import Any

from app.engines.base import SchemaBundle, ValidationFinding, ValidationResult


_ALLOWED_TOP_KEYS = frozenset(
    {
        "endpoint",
        "method",
        "path_params",
        "query_params",
        "headers",
        "json_path",
        "row_field_paths",
    }
)


def validate_api_query(
    envelope_str: str,
    *,
    schema_bundle: SchemaBundle | None = None,
) -> tuple[ValidationResult, dict[str, Any] | None]:
    """Validate a REST-API envelope. Returns the result + parsed envelope.

    On failure ``ValidationResult.ok == False`` and the second tuple
    element is ``None``. On success ``rewritten_sql`` holds the
    canonical (key-sorted) JSON form so the engine can ship a
    deterministic envelope to the wire.
    """
    findings: list[ValidationFinding] = []

    try:
        env = json.loads(envelope_str)
    except (ValueError, TypeError) as e:
        # Bandaid? No — the planner can return malformed JSON when
        # the LLM hiccups; converting parse failure into a structured
        # finding is the validator's whole job.
        findings.append(
            ValidationFinding(
                code="api_invalid_json",
                message=f"envelope is not valid JSON: {e}",
            )
        )
        return ValidationResult(ok=False, findings=findings), None

    if not isinstance(env, dict):
        findings.append(
            ValidationFinding(
                code="api_invalid_json",
                message="envelope root must be a JSON object",
            )
        )
        return ValidationResult(ok=False, findings=findings), None

    # Unknown top-level keys: warn but don't reject — let future
    # extensions land without a flag day.
    extra = set(env) - _ALLOWED_TOP_KEYS
    if extra:
        findings.append(
            ValidationFinding(
                code="api_unknown_keys",
                message=f"unknown envelope keys (ignored): {sorted(extra)}",
            )
        )

    # endpoint must exist and be a non-empty string starting with "/".
    endpoint = env.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        findings.append(
            ValidationFinding(
                code="api_missing_endpoint",
                message="envelope must include an 'endpoint' string",
            )
        )
        return ValidationResult(ok=False, findings=findings), None
    if endpoint.startswith(("http://", "https://", "//")):
        findings.append(
            ValidationFinding(
                code="api_absolute_url",
                message=(
                    "endpoint must be a relative path; the engine "
                    "prepends base_url. Absolute URLs are blocked to "
                    "prevent SSRF via planner output."
                ),
            )
        )
        return ValidationResult(ok=False, findings=findings), None
    if not endpoint.startswith("/"):
        findings.append(
            ValidationFinding(
                code="api_invalid_endpoint",
                message="endpoint must start with '/'",
            )
        )
        return ValidationResult(ok=False, findings=findings), None
    if ".." in endpoint.split("/"):
        findings.append(
            ValidationFinding(
                code="api_path_traversal",
                message="endpoint must not contain '..' segments",
            )
        )
        return ValidationResult(ok=False, findings=findings), None

    # method must be GET.
    method_raw = env.get("method")
    if not isinstance(method_raw, str):
        findings.append(
            ValidationFinding(
                code="api_missing_method",
                message="envelope must include a 'method' string",
            )
        )
        return ValidationResult(ok=False, findings=findings), None
    method = method_raw.upper()
    if method != "GET":
        findings.append(
            ValidationFinding(
                code="api_method_not_get",
                message=(
                    f"method '{method_raw}' is not allowed — REST API "
                    "connections are read-only; only GET is permitted."
                ),
            )
        )
        return ValidationResult(ok=False, findings=findings), None

    # Optional fields — shape-check what's present.
    for key in ("path_params", "query_params", "headers"):
        v = env.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            findings.append(
                ValidationFinding(
                    code="api_invalid_param_type",
                    message=f"'{key}' must be a JSON object",
                )
            )
            return ValidationResult(ok=False, findings=findings), None
        for k, val in v.items():
            if not isinstance(k, str):
                findings.append(
                    ValidationFinding(
                        code="api_invalid_param_type",
                        message=f"'{key}' keys must be strings",
                    )
                )
                return ValidationResult(ok=False, findings=findings), None
            if not _is_scalar_or_scalar_list(val):
                findings.append(
                    ValidationFinding(
                        code="api_invalid_param_type",
                        message=(
                            f"'{key}.{k}' must be a scalar or a list "
                            "of scalars (no nested objects/arrays)"
                        ),
                    )
                )
                return ValidationResult(ok=False, findings=findings), None

    jp = env.get("json_path")
    if jp is not None and not isinstance(jp, str):
        findings.append(
            ValidationFinding(
                code="api_invalid_json_path",
                message="'json_path' must be a string",
            )
        )
        return ValidationResult(ok=False, findings=findings), None

    rfp = env.get("row_field_paths")
    if rfp is not None:
        if not isinstance(rfp, dict):
            findings.append(
                ValidationFinding(
                    code="api_invalid_row_field_paths",
                    message="'row_field_paths' must be a JSON object",
                )
            )
            return ValidationResult(ok=False, findings=findings), None
        for k, v in rfp.items():
            if not isinstance(k, str) or not isinstance(v, str):
                findings.append(
                    ValidationFinding(
                        code="api_invalid_row_field_paths",
                        message="'row_field_paths' must map string→string",
                    )
                )
                return ValidationResult(ok=False, findings=findings), None

    # Endpoint catalog check — only when we have a schema bundle to
    # check against. The catalog stores endpoint paths under TableMeta.name
    # with slashes / braces escaped to '_' so they're valid identifiers.
    # Compare against the ORIGINAL path (stored in foreign_keys[0] or in
    # a side dict) — we encode the original in the schema bundle by
    # using ``"{method} {path}"`` as the table name (see rest_api engine).
    if schema_bundle is not None and schema_bundle.dialect == "rest_api":
        catalog_paths = _catalog_paths(schema_bundle)
        if catalog_paths and not _endpoint_matches(endpoint, catalog_paths):
            findings.append(
                ValidationFinding(
                    code="api_endpoint_not_in_catalog",
                    message=(
                        f"endpoint {endpoint!r} is not in the introspected "
                        "API catalog; the planner must pick a path that "
                        "matches one of the schema tables."
                    ),
                )
            )
            return ValidationResult(ok=False, findings=findings), None

    # Canonical re-serialisation with sorted keys → stable wire payload
    # downstream (helps with caching and tests).
    rewritten = json.dumps(env, sort_keys=True)
    return (
        ValidationResult(ok=True, rewritten_sql=rewritten, findings=findings),
        env,
    )


# ── helpers ────────────────────────────────────────────────────────


def _is_scalar_or_scalar_list(v: Any) -> bool:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return True
    if isinstance(v, list):
        return all(
            isinstance(item, (str, int, float, bool)) or item is None
            for item in v
        )
    return False


def _catalog_paths(bundle: SchemaBundle) -> list[str]:
    """Return the original (unescaped) path templates registered in the
    REST API engine's introspected schema bundle.

    The engine encodes paths as ``"GET {path}"`` in ``TableMeta.name``
    so reverse-engineering them is straightforward: split off the
    leading ``"GET "``.
    """
    out: list[str] = []
    for t in bundle.tables:
        if t.name.startswith("GET "):
            out.append(t.name[len("GET ") :])
    return out


def _endpoint_matches(actual: str, templates: list[str]) -> bool:
    """Does ``actual`` match one of the catalog templates?

    Template segments wrapped in ``{...}`` match any single non-empty
    segment in the actual path. Everything else must match literally.
    Trailing slash differences are tolerated.
    """
    a = _split_path(actual)
    for tmpl in templates:
        t = _split_path(tmpl)
        if len(a) != len(t):
            continue
        ok = True
        for seg_a, seg_t in zip(a, t):
            if seg_t.startswith("{") and seg_t.endswith("}"):
                if not seg_a:
                    ok = False
                    break
                continue
            if seg_a != seg_t:
                ok = False
                break
        if ok:
            return True
    return False


def _split_path(p: str) -> list[str]:
    return [seg for seg in p.strip("/").split("/")]
