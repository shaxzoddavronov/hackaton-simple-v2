"""Read-only validator for MongoDB aggregation pipelines.

Mirrors :mod:`app.services.es_readonly_validator` (and the SQL-level
sqlglot validator) but operates on the JSON envelope that the planner
emits for the Mongo dialect.

Envelope shape:

    {
      "database":   "<db_name>",
      "collection": "<coll_name>",
      "pipeline":   [ { "$match": {...} }, { "$group": {...} }, ... ]
    }

Single-collection aggregation only. The engine forwards the pipeline
verbatim to ``db[coll].aggregate(pipeline)``.

Security boundaries enforced here:

  * **Banned operators** anywhere in the document, regardless of
    nesting depth: ``$out``, ``$merge`` (write stages),
    ``$function``, ``$accumulator``, ``$where`` (arbitrary JS
    execution).
  * **System pipeline stages** — ``$indexStats``, ``$collStats``,
    ``$listSampledQueries``, ``$listLocalSessions``.
  * **System collections** — anything under the ``admin``, ``config``,
    or ``local`` databases, plus collection names matching
    ``system.*`` (``system.users``, ``system.roles``,
    ``system.profile``, …).
  * **Allow-list of pipeline stages** — anything outside the
    allow-list is rejected (defense in depth against future Mongo
    stages that introduce side effects).
  * **Default ``$limit``** — if the planner forgets to cap output,
    a sentinel ``$limit: 1000`` is appended.

The validator returns ``(ValidationResult, parsed_envelope_or_None)``
just like the ES counterpart so callers can reuse the cleaned envelope
without re-parsing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.engines.base import ValidationFinding, ValidationResult


# Operator keys whose presence ANYWHERE in the document is a hard
# rejection (write side effects or arbitrary code execution).
_BANNED_KEYS = frozenset(
    {
        "$out",
        "$merge",
        "$function",
        "$accumulator",
        "$where",
    }
)

# Pipeline STAGE prefixes that we never allow. These differ from
# `_BANNED_KEYS` because some of them (`$collStats`) are only banned as
# top-level pipeline stages — but for simplicity (and because users
# never need them) we deep-walk-reject them outright.
_BANNED_STAGES = frozenset(
    {
        "$out",
        "$merge",
        "$indexStats",
        "$collStats",
        "$listSampledQueries",
        "$listLocalSessions",
        "$planCacheStats",
        "$currentOp",
    }
)

# Allow-list of pipeline stages. Anything else gets rejected. We err on
# the side of fewer stages — adding a stage later is a one-line change.
_ALLOWED_STAGES = frozenset(
    {
        "$match",
        "$group",
        "$project",
        "$sort",
        "$limit",
        "$skip",
        "$count",
        "$unwind",
        "$lookup",
        "$facet",
        "$bucket",
        "$bucketAuto",
        "$addFields",
        "$set",
        "$replaceRoot",
        "$replaceWith",
        "$densify",
        "$fill",
        "$redact",
        "$sortByCount",
    }
)

# Reserved Mongo databases. Touching them is always a no.
_SYSTEM_DATABASES = frozenset({"admin", "config", "local"})

# Default row cap injected as a trailing `$limit` when the planner
# forgot to bound output. Matches the SQL row_cap default.
_DEFAULT_LIMIT = 1000


@dataclass(slots=True)
class _Walk:
    findings: list[ValidationFinding] = field(default_factory=list)


def validate_mongo_query(
    raw: str | dict[str, Any],
) -> tuple[ValidationResult, dict[str, Any] | None]:
    """Validate a Mongo aggregation envelope.

    Returns ``(result, parsed_envelope)``. ``parsed_envelope`` is
    ``None`` when the JSON itself was invalid; on success it is the
    cleaned envelope with a default ``$limit`` appended if needed.
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

    database = envelope.get("database")
    collection = envelope.get("collection")
    pipeline = envelope.get("pipeline")

    if not isinstance(database, str) or not database.strip():
        w.findings.append(
            ValidationFinding(
                code="DATABASE_REQUIRED",
                message="Envelope must include a non-empty 'database' string",
            )
        )
    if not isinstance(collection, str) or not collection.strip():
        w.findings.append(
            ValidationFinding(
                code="COLLECTION_REQUIRED",
                message="Envelope must include a non-empty 'collection' string",
            )
        )
    if not isinstance(pipeline, list):
        w.findings.append(
            ValidationFinding(
                code="ENVELOPE_SHAPE",
                message="Envelope 'pipeline' must be a JSON array",
            )
        )
        return ValidationResult(ok=False, findings=w.findings), None

    # System DB / collection checks.
    if isinstance(database, str):
        if database in _SYSTEM_DATABASES:
            w.findings.append(
                ValidationFinding(
                    code="SYSTEM_COLLECTION",
                    message=(
                        f"Access to system database '{database}' is not allowed"
                    ),
                )
            )
    if isinstance(collection, str):
        if collection.startswith("system."):
            w.findings.append(
                ValidationFinding(
                    code="SYSTEM_COLLECTION",
                    message=(
                        f"Access to system collection '{collection}' is not allowed"
                    ),
                )
            )

    # Per-stage allow-list + deep-walk banned operator check.
    for idx, stage in enumerate(pipeline):
        if not isinstance(stage, dict) or len(stage) != 1:
            w.findings.append(
                ValidationFinding(
                    code="ENVELOPE_SHAPE",
                    message=(
                        f"Pipeline stage #{idx} must be a single-key object "
                        f"like {{'$match': {{...}}}}"
                    ),
                )
            )
            continue

        (stage_name,) = stage.keys()
        if stage_name in _BANNED_STAGES:
            w.findings.append(
                ValidationFinding(
                    code="BANNED_STAGE",
                    message=(
                        f"Pipeline stage '{stage_name}' is not allowed "
                        "(write or system stage)"
                    ),
                )
            )
            continue
        if stage_name not in _ALLOWED_STAGES:
            w.findings.append(
                ValidationFinding(
                    code="BANNED_STAGE",
                    message=(
                        f"Pipeline stage '{stage_name}' is not in the "
                        "read-only allow-list"
                    ),
                )
            )
            continue
        # Deep walk for banned operators within the stage body.
        _scan(stage[stage_name], w)

    if w.findings:
        return ValidationResult(ok=False, findings=w.findings), None

    # Inject a default $limit when none is set anywhere in the pipeline.
    # We check by stage name only — a `$limit` inside a `$facet` still
    # counts as bounded because facet branches are independently capped
    # by Mongo at 16MB document size.
    has_limit = any(
        isinstance(s, dict) and "$limit" in s for s in pipeline
    )
    if not has_limit:
        pipeline.append({"$limit": _DEFAULT_LIMIT})
        envelope["pipeline"] = pipeline

    return (
        ValidationResult(ok=True, rewritten_sql=json.dumps(envelope)),
        envelope,
    )


def _scan(node: Any, w: _Walk) -> None:
    """Recursive walk that rejects any banned operator key, anywhere.

    Catches things like ``$lookup`` with a sub-pipeline that contains
    ``$out`` — the outer ``$lookup`` is on the allow-list, but the
    nested write is rejected by this walker.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _BANNED_KEYS:
                w.findings.append(
                    ValidationFinding(
                        code="BANNED_KEY",
                        message=(
                            f"Operator '{k}' is not allowed "
                            "(write / arbitrary code execution)"
                        ),
                    )
                )
            if k in _BANNED_STAGES and k not in _BANNED_KEYS:
                # `$out` / `$merge` appear in both sets; reported once
                # by the BANNED_KEY branch. Other stages (e.g.
                # `$indexStats`) hit this branch only when nested in
                # a `$lookup` sub-pipeline.
                w.findings.append(
                    ValidationFinding(
                        code="BANNED_STAGE",
                        message=(
                            f"Stage '{k}' is not allowed in nested pipelines"
                        ),
                    )
                )
            _scan(v, w)
    elif isinstance(node, list):
        for item in node:
            _scan(item, w)


__all__ = ["validate_mongo_query"]
