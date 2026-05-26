from __future__ import annotations

import logging

from pydantic import ValidationError

from app.agents.llm import get_llm
from app.agents.state import GraphState
from app.engines.base import SchemaBundle
from app.schemas.llm_io import SqlPlan

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a SQL planner for a strict READ-ONLY analytics tool. "
    "Generate exactly one SELECT (with optional WITH/CTEs) that answers "
    "the user's question against the provided schema. "
    "Rules: SELECT only, no DML/DDL, no system tables, no functions like "
    "pg_sleep or load_file. Reference only columns that exist. "
    "Prefer concise queries; do not include comments."
)

_MAX_RAG_CHARS = 1800


def _rag_context(chunks: list[dict[str, object]]) -> str:
    """Compose non-schema RAG chunks (API + docs) into a compact context block."""
    if not chunks:
        return ""
    parts: list[str] = []
    used = 0
    for c in chunks:
        kind = str(c.get("kind", ""))
        if kind in {"", "schema_table", "schema_column"}:
            continue
        snippet = str(c.get("text", "")).strip()
        if not snippet:
            continue
        block = f"[{kind} :: {c.get('source_key','')}]\n{snippet}"
        if used + len(block) > _MAX_RAG_CHARS:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def _schema_brief(bundle: SchemaBundle | None, keep: list[str] | None) -> str:
    if bundle is None:
        return "(no schema loaded)"
    keep_set = set(keep or [])
    lines: list[str] = [f"dialect={bundle.dialect}"]
    for t in bundle.tables:
        qn = f"{t.schema}.{t.name}"
        if keep_set and qn not in keep_set:
            continue
        cols = ", ".join(f"{c.name}:{c.data_type}" for c in t.columns)
        line = f"- {qn}({cols})"
        if t.foreign_keys:
            fks = "; ".join(
                f"{','.join(fk.from_columns)}->{fk.to_table}({','.join(fk.to_columns)})"
                for fk in t.foreign_keys
            )
            line += f"  fks: {fks}"
        lines.append(line)
    # Categorical samples help the planner pick the right values.
    sample_lines: list[str] = []
    for qn, cols in bundle.samples.items():
        if keep_set and qn not in keep_set:
            continue
        for cname, s in cols.items():
            if s.distinct_values:
                vals = ", ".join(repr(v) for v in s.distinct_values[:8])
                sample_lines.append(f"  {qn}.{cname} in {{ {vals}{', ...' if s.distinct_truncated else ''} }}")
    if sample_lines:
        lines.append("samples:")
        lines.extend(sample_lines[:30])
    return "\n".join(lines)


async def run(state: GraphState) -> GraphState:
    attempts = int(state.get("planner_attempts", 0)) + 1
    bundle = state.get("schema_bundle")
    keep = state.get("pruned_table_qnames")

    feedback: list[str] = []
    if state.get("last_validation_error"):
        feedback.append(f"Previous attempt rejected by validator: {state['last_validation_error']}")
    if state.get("last_executor_error"):
        feedback.append(f"Previous attempt failed at execution: {state['last_executor_error']}")

    # Append non-schema RAG context (API endpoints, user docs). Schema chunks
    # are already represented by `_schema_brief` via `pruned_table_qnames`,
    # so we drop those to avoid duplication.
    rag_extras = _rag_context(state.get("retrieved_chunks") or [])

    prompt_user = (
        f"Question: {state.get('user_message','')}\n\n"
        f"Schema:\n{_schema_brief(bundle, keep)}\n\n"
        + (f"Reference context:\n{rag_extras}\n\n" if rag_extras else "")
        + ("\n".join(feedback) + "\n\n" if feedback else "")
        + "Return a SqlPlan."
    )

    llm = get_llm()
    try:
        plan = await llm.structured(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt_user},
            ],
            SqlPlan,
        )
    except ValidationError as e:
        # LLMClient already tried salvage + a repair turn. We still
        # failed schema validation — feed the failure back into the
        # retry loop with a concrete message so the next attempt can
        # course-correct. Returning an empty plan steers
        # `_route_after_validation` to either retry or, on exhaustion,
        # error_responder. Note: no ``plan`` field is set, so validator
        # sees nothing to validate and the router checks attempts.
        err_summary = "; ".join(
            f"{'.'.join(str(x) for x in (it.get('loc') or []))}: {it.get('msg')}"
            for it in e.errors()[:3]
        ) or str(e)[:300]
        log.warning("planner: schema validation failed after repair: %s", err_summary)
        return {
            "planner_attempts": attempts,
            "plan": None,
            "validation": None,
            "last_validation_error": (
                "LLM returned JSON that doesn't match the SqlPlan schema. "
                f"Errors: {err_summary}. Try again — return ONLY a single "
                "JSON object with keys: dialect, sql, rationale, expected_columns."
            ),
            "last_executor_error": None,
        }
    return {
        "plan": plan,
        "planner_attempts": attempts,
        # Clear stale feedback for the next turn
        "last_validation_error": None,
        "last_executor_error": None,
    }
