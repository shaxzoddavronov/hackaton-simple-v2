"""Read-only validator for Elasticsearch Query DSL.

Mirrors ``services.readonly_validator`` (which uses sqlglot on SQL) but
operates on the JSON request shape that ES expects. The validator is
the security boundary: even if our agent's LLM tries to write a script
that calls ``Painless`` or a request that includes ``_delete_by_query``,
this layer rejects it before we send anything over the wire.

The agent emits queries as an envelope:

    {
      "index": "logs-*",
      "body":  { ... ES request body ... }
    }

The body must use only an allow-listed set of top-level keys (search +
aggregations only). We walk the body recursively and reject any node
named ``script`` — Painless scripts can execute arbitrary code under
the ES cluster's permissions, so they're banned wholesale.

We also impose a size cap (the agent might forget) and a default
timeout if the model didn't set one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.engines.base import ValidationFinding, ValidationResult

# Search/aggregation keys we permit at the top of `body`.
_ALLOWED_BODY_KEYS = frozenset(
    {
        "query",
        "aggs",
        "aggregations",
        "size",
        "from",
        "sort",
        "_source",
        "fields",
        "track_total_hits",
        "timeout",
        "post_filter",
        "highlight",
        "search_after",
        "stored_fields",
        "docvalue_fields",
        "min_score",
        "explain",
        "version",
        "seq_no_primary_term",
        "terminate_after",
        "indices_boost",
        "knn",
        "rescore",
        "collapse",
        # Suggester is read-only.
        "suggest",
    }
)

# Anywhere in the body, these keys signal scripting / mutation and are
# rejected hard. Even deep inside a ``script_score`` clause.
_BANNED_KEYS = frozenset(
    {
        "script",
        "script_fields",
        "script_score",
        "scripted_metric",
        "runtime_mappings",
        "_delete_by_query",
        "_update_by_query",
        "_reindex",
        "snapshot",
        "restore",
    }
)

# Index-name patterns we refuse to touch. Hidden / dot-prefixed indices
# are infrastructure (security, ml, monitoring). Wildcards that would
# match them are also blocked.
_BANNED_INDEX_PATTERNS = (
    ".security",
    ".kibana",
    ".ml",
    ".monitoring",
    ".watches",
    ".tasks",
    ".async-search",
    ".transform",
    ".fleet",
)

# Hard cap on size; protects against accidental "return everything".
_MAX_SIZE = 1000


@dataclass(slots=True)
class _Walk:
    findings: list[ValidationFinding] = field(default_factory=list)


def validate_es_query(raw: str | dict[str, Any]) -> tuple[ValidationResult, dict[str, Any] | None]:
    """Validate the JSON envelope. Return (result, parsed envelope).

    The envelope is ``{"index": "...", "body": {...}}``. On failure the
    parsed envelope may be ``None`` (e.g. when JSON itself is invalid).
    On success the envelope is the cleaned, ready-to-send dict (size
    cap applied, default timeout added).
    """
    w = _Walk()

    if isinstance(raw, str):
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as e:
            w.findings.append(
                ValidationFinding(code="PARSE_ERROR", message=f"Invalid JSON: {e}")
            )
            return ValidationResult(ok=False, findings=w.findings), None
    else:
        envelope = raw

    if not isinstance(envelope, dict):
        w.findings.append(
            ValidationFinding(
                code="ENVELOPE_SHAPE",
                message="Envelope must be a JSON object",
            )
        )
        return ValidationResult(ok=False, findings=w.findings), None

    index = envelope.get("index")
    body = envelope.get("body")

    if not isinstance(index, str) or not index.strip():
        w.findings.append(
            ValidationFinding(
                code="INDEX_REQUIRED",
                message="Envelope must include a non-empty 'index' string",
            )
        )

    if not isinstance(body, dict):
        w.findings.append(
            ValidationFinding(
                code="BODY_SHAPE",
                message="Envelope must include a 'body' object (the ES request body)",
            )
        )
        return ValidationResult(ok=False, findings=w.findings), None

    # Index allow-list — block dot-prefixed system indices.
    if isinstance(index, str):
        for pat in _BANNED_INDEX_PATTERNS:
            if pat in index:
                w.findings.append(
                    ValidationFinding(
                        code="SYSTEM_INDEX",
                        message=f"Access to system index pattern '{pat}' is not allowed",
                    )
                )
                break
        if index.startswith("."):
            w.findings.append(
                ValidationFinding(
                    code="SYSTEM_INDEX",
                    message="Hidden / dot-prefixed indices are not allowed",
                )
            )

    # Top-level keys must be in the allow-list. Aggregations get the
    # alias "aggregations" treatment.
    for k in list(body.keys()):
        if k not in _ALLOWED_BODY_KEYS:
            w.findings.append(
                ValidationFinding(
                    code="BODY_KEY_NOT_ALLOWED",
                    message=f"Top-level body key '{k}' is not allowed",
                )
            )

    # Deep walk for banned keys (script, runtime_mappings, etc.).
    _scan(body, w)

    if w.findings:
        return ValidationResult(ok=False, findings=w.findings), None

    # Inject a sensible default size when missing or oversized.
    size = body.get("size")
    if not isinstance(size, int) or size < 0:
        # When the body has aggs and no explicit size, ES defaults to 10
        # which wastes bandwidth; force 0 so only aggs come back.
        body["size"] = 0 if ("aggs" in body or "aggregations" in body) else 50
    elif size > _MAX_SIZE:
        body["size"] = _MAX_SIZE

    # Default request timeout — protects against runaway aggs.
    body.setdefault("timeout", "10s")

    return ValidationResult(ok=True, rewritten_sql=json.dumps(envelope)), envelope


def _scan(node: Any, w: _Walk) -> None:
    """Recursive walk that rejects any banned key, anywhere."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _BANNED_KEYS:
                w.findings.append(
                    ValidationFinding(
                        code="BANNED_KEY",
                        message=f"Key '{k}' is not allowed (script/mutation/system)",
                    )
                )
            _scan(v, w)
    elif isinstance(node, list):
        for item in node:
            _scan(item, w)


__all__ = ["validate_es_query"]
