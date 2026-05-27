from __future__ import annotations

from app.agents.llm import get_llm
from app.agents.state import GraphState
from app.engines.base import ResultSet, SchemaBundle
from app.schemas.llm_io import AnswerDraft
from app.services.rag.citations import (
    build_citations,
    citation_hint_for_planner,
)

_SYSTEM = (
    "You are an analyst who writes 2-3 sentence summaries of SQL results. "
    "Use only the numbers and labels you are shown. Do not invent values. "
    "Highlight the key takeaway in the headline; back it up in body_md.\n"
    "\n"
    "LANGUAGE — STRICT: respond in the SAME LANGUAGE the user wrote "
    "their most recent message in. If the user wrote in Uzbek, answer in "
    "Uzbek. If in English, answer in English. If in Russian, answer in "
    "Russian. Never switch to a different language just because the "
    "prior turn was in English."
)

# Used for chitchat / metadata intents. The metadata branch additionally
# pastes the actual schema in the user turn — without it the model
# happily invents tables like ``user_activities`` that don't exist.
_META_SYSTEM = (
    "You answer questions about a connected database. You will be shown "
    "the database schema. ONLY reference tables and columns that appear "
    "in the schema. Never invent names. If the schema cannot answer the "
    "question, say so and suggest the closest alternative.\n"
    "\n"
    "LANGUAGE — STRICT: respond in the SAME LANGUAGE the user wrote "
    "their question in. Uzbek question → Uzbek answer. English → "
    "English. Russian → Russian. NEVER answer in a different language.\n"
    "\n"
    "Length rules — STRICT:\n"
    "  * headline: ONE short sentence (max 12 words).\n"
    "  * body_md: 2-4 short sentences. NO bullet lists. NO tables. "
    "NO long enumerations of every column. If you must list things, "
    "name at most 5 items inline and write 'and others' if there are "
    "more. Total body_md MUST stay under 600 characters.\n"
    "  * key_numbers: omit unless a concrete number from the schema "
    "answers the question directly.\n"
    "Total response (headline + body_md) must comfortably fit in a "
    "chat bubble — do not exceed 800 characters combined."
)


def _result_shape(rs: ResultSet | None) -> str:
    if rs is None:
        return "no result"
    return (
        f"columns: {rs.columns}\n"
        f"row_count: {rs.row_count}\n"
        f"sample_rows (first 5): {rs.rows[:5]}\n"
    )


def _schema_brief(bundle: SchemaBundle | None) -> str:
    """Compact schema listing for the metadata-answer prompt.

    Mirrors :func:`agents.nodes.query_planner._schema_brief` but without
    samples — for metadata Q&A we want a complete table/column catalog,
    not the planner's pruned subset.
    """
    if bundle is None or not bundle.tables:
        return "(no schema available)"
    lines: list[str] = [f"dialect={bundle.dialect}"]
    for t in bundle.tables:
        cols = ", ".join(f"{c.name}:{c.data_type}" for c in t.columns)
        line = f"- {t.schema}.{t.name}({cols})"
        if t.foreign_keys:
            fks = "; ".join(
                f"{','.join(fk.from_columns)}->{fk.to_table}"
                f"({','.join(fk.to_columns)})"
                for fk in t.foreign_keys
            )
            line += f"  fks: {fks}"
        lines.append(line)
    return "\n".join(lines)


async def run(state: GraphState) -> GraphState:
    rs = state.get("result")
    intent = state.get("intent")

    history = state.get("conversation_history") or []

    # Build the citation digest once — used by all three branches
    # (metadata / chitchat with docs / data result).
    citations = build_citations(state.get("retrieved_chunks") or [])
    citation_hint = citation_hint_for_planner(citations)

    def _with_history(system: str, user_prompt: str) -> list[dict[str, object]]:
        msgs: list[dict[str, object]] = [{"role": "system", "content": system}]
        for h in history[-6:]:
            role = h.get("role", "user")
            content = h.get("content", "")
            if role not in ("user", "assistant") or not content:
                continue
            msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": user_prompt})
        return msgs

    def _append_citation_hint(prompt: str) -> str:
        if not citation_hint:
            return prompt
        return (
            f"{prompt}\n\n{citation_hint}\n\n"
            "If you reference a source, cite by [number] inline — e.g. "
            "'Per the HR handbook [1], …'. Stay concise; do not list all "
            "sources unless asked."
        )

    # ── Metadata / chitchat path ─────────────────────────────────────
    if rs is None and intent in {"chitchat", "metadata"}:
        llm = get_llm()
        bundle = state.get("schema_bundle")

        if intent == "metadata":
            schema_text = _schema_brief(bundle)
            user_prompt = (
                f"Schema:\n{schema_text}\n\n"
                f"User question: {state.get('user_message','')}\n\n"
                "Answer using ONLY the tables and columns in the schema "
                "above. Match the user's language. Return an AnswerDraft."
            )
            system_prompt = _META_SYSTEM
        else:
            # chitchat — no schema, but still match language + see history.
            user_prompt = state.get("user_message", "")
            system_prompt = (
                "You answer brief conversational questions about a "
                "NL-to-SQL tool called QueryMind. Keep it short and "
                "helpful.\n\n"
                "LANGUAGE — STRICT: respond in the SAME LANGUAGE the "
                "user wrote their question in. Uzbek → Uzbek, English "
                "→ English, Russian → Russian. Never switch."
            )

        # AnswerDraft for metadata can run long when the model lists
        # tables/columns verbosely. 2048 leaves headroom over the
        # ~800-char target so we don't truncate mid-string and trip
        # the salvage layer.
        draft = await llm.structured(
            _with_history(system_prompt, _append_citation_hint(user_prompt)),
            AnswerDraft,
            max_tokens=2048,
        )
        return {"answer": draft, "citations": citations}

    if rs is None:
        return {
            "answer": AnswerDraft(
                headline="No result.",
                body_md="The query returned no rows.",
            ),
            "citations": citations,
        }

    llm = get_llm()
    prompt = (
        f"Question: {state.get('user_message','')}\n\n"
        f"Result shape:\n{_result_shape(rs)}\n\n"
        "Return an AnswerDraft in the user's language."
    )
    draft = await llm.structured(
        _with_history(_SYSTEM, _append_citation_hint(prompt)),
        AnswerDraft,
        max_tokens=2048,
    )
    return {"answer": draft, "citations": citations}
