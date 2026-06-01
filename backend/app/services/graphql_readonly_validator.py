"""Phase 32 — GraphQL JSON-envelope validator.

Standalone counterpart to :mod:`app.services.readonly_validator` and
:mod:`app.services.api_query_validator`. The agent calls this BEFORE
the engine ever opens a connection — defense in depth at the parse
layer, so a misbehaving planner output is rejected before it hits
the network.

Validation rules:
  1. Envelope must be ``{"query": "...", "variables": {...}}``.
  2. ``query`` must be parseable as a GraphQL document.
  3. EVERY operation in the document must be a ``query`` (no
     ``mutation``, no ``subscription``).
  4. Anonymous operations are allowed; named operations are allowed.

Returns ``(ValidationResult, parsed_envelope)``. The envelope is
returned so callers can reuse it without re-parsing JSON.
"""
from __future__ import annotations

import json
from typing import Any

from app.engines.base import ValidationFinding, ValidationResult


def validate_graphql_query(
    raw_sql: str,
) -> tuple[ValidationResult, dict[str, Any] | None]:
    try:
        envelope = json.loads(raw_sql)
    except (ValueError, TypeError) as e:
        return (
            ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_invalid_envelope",
                        message=f"envelope is not valid JSON: {e}",
                    )
                ],
            ),
            None,
        )
    if not isinstance(envelope, dict):
        return (
            ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_invalid_envelope",
                        message="envelope must be a JSON object",
                    )
                ],
            ),
            None,
        )
    query = envelope.get("query")
    if not isinstance(query, str) or not query.strip():
        return (
            ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_missing_query",
                        message="envelope.query is required",
                    )
                ],
            ),
            envelope,
        )
    variables = envelope.get("variables")
    if variables is not None and not isinstance(variables, dict):
        return (
            ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_bad_variables",
                        message="envelope.variables must be a JSON object",
                    )
                ],
            ),
            envelope,
        )

    try:
        from graphql import parse as gql_parse
        from graphql.language.ast import OperationDefinitionNode
    except ImportError:
        lower = query.lower()
        if "mutation" in lower or "subscription" in lower:
            return (
                ValidationResult(
                    ok=False,
                    findings=[
                        ValidationFinding(
                            code="graphql_write_operation",
                            message=(
                                "mutation/subscription rejected; install "
                                "graphql-core for AST validation"
                            ),
                        )
                    ],
                ),
                envelope,
            )
        return (
            ValidationResult(ok=True, rewritten_sql=raw_sql),
            envelope,
        )

    try:
        ast = gql_parse(query)
    except Exception as e:
        return (
            ValidationResult(
                ok=False,
                findings=[
                    ValidationFinding(
                        code="graphql_parse_error",
                        message=f"failed to parse GraphQL: {e}",
                    )
                ],
            ),
            envelope,
        )

    findings: list[ValidationFinding] = []
    saw_operation = False
    for defn in ast.definitions:
        if isinstance(defn, OperationDefinitionNode):
            saw_operation = True
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
    if not saw_operation:
        findings.append(
            ValidationFinding(
                code="graphql_no_operation",
                message="document contains no executable operation",
            )
        )
    if findings:
        return ValidationResult(ok=False, findings=findings), envelope
    return ValidationResult(ok=True, rewritten_sql=raw_sql), envelope
