from __future__ import annotations

from app.agents.llm import get_llm
from app.agents.state import GraphState
from app.engines.base import ResultSet, SchemaBundle
from app.schemas.llm_io import AnswerDraft

_SYSTEM = (
    "You are an analyst who writes 2-3 sentence summaries of SQL results. "
    "Use only the numbers and labels you are shown. Do not invent values. "
    "Highlight the key takeaway in the headline; back it up in body_md."
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

    # ── Metadata / chitchat path ─────────────────────────────────────
    if rs is None and intent in {"chitchat", "metadata"}:
        llm = get_llm()
        bundle = state.get("schema_bundle")

        if intent == "metadata":
            schema_text = _schema_brief(bundle)
            user_prompt = (
                f"Schema:\n{schema_text}\n\n"
                f"User question: {state.get('user_message','')}\n\n"
                "Answer the question using ONLY the tables and columns in "
                "the schema above. Return an AnswerDraft."
            )
            system_prompt = _META_SYSTEM
        else:
            # chitchat — no schema needed
            user_prompt = state.get("user_message", "")
            system_prompt = (
                "You answer brief conversational questions about a NL-to-SQL "
                "tool called QueryMind. Keep it short and helpful."
            )

        # AnswerDraft for metadata can run long when the model lists
        # tables/columns verbosely. 2048 leaves headroom over the
        # ~800-char target so we don't truncate mid-string and trip
        # the salvage layer.
        draft = await llm.structured(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            AnswerDraft,
            max_tokens=2048,
        )
        return {"answer": draft}

    if rs is None:
        return {"answer": AnswerDraft(headline="No result.", body_md="The query returned no rows.")}

    llm = get_llm()
    prompt = (
        f"Question: {state.get('user_message','')}\n\n"
        f"Result shape:\n{_result_shape(rs)}\n\n"
        "Return an AnswerDraft."
    )
    draft = await llm.structured(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        AnswerDraft,
        max_tokens=2048,
    )
    return {"answer": draft}
