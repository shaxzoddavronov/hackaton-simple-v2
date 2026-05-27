from __future__ import annotations

import logging

from pydantic import ValidationError

from app.agents.llm import get_llm
from app.agents.state import GraphState
from app.engines.base import SchemaBundle
from app.schemas.llm_io import SqlPlan

log = logging.getLogger(__name__)

_SQL_SYSTEM = (
    "You are a SQL planner for a strict READ-ONLY analytics tool. "
    "Generate exactly one SELECT (with optional WITH/CTEs) that answers "
    "the user's question against the provided schema.\n"
    "\n"
    "Conversation context — IMPORTANT:\n"
    "  When prior turns are present, treat short messages like "
    "'show as chart' / 'grafik korinishda korsat' / 'more detail' / "
    "'aniq ber' as REFERENCES to the previous data question. Re-emit a "
    "SQL that answers the previous user question — do NOT pick a "
    "different table just because the new phrase mentions something "
    "off-topic. The chart_designer is what changes the visualization; "
    "you write the same SQL.\n"
    "\n"
    "Rules:\n"
    "  * SELECT only, no DML/DDL, no system tables, no pg_sleep/load_file.\n"
    "  * Reference ONLY tables and columns that appear in the schema.\n"
    "  * Do NOT invent column names. If the question can't be answered\n"
    "    with the given schema, write the closest meaningful SELECT and\n"
    "    say so in the rationale.\n"
    "  * Do NOT add time filters the user did not ask for. If the user\n"
    "    says 'last 30 days', filter by 30 days; if they say nothing\n"
    "    about time, do NOT inject an arbitrary INTERVAL.\n"
    "  * Concise queries, no SQL comments.\n"
    "\n"
    "Display-name handling — IMPORTANT:\n"
    "  When the result will surface a person/entity name, fall back\n"
    "  gracefully if some rows have NULL or empty names. Combine the\n"
    "  available identifier columns so the UI always has something\n"
    "  meaningful to show. Examples:\n"
    "    * users(id, full_name, username) →\n"
    "        COALESCE(NULLIF(TRIM(u.full_name), ''), u.username,\n"
    "                 'user #' || u.id::text) AS display_name\n"
    "    * customers(id, name, email) →\n"
    "        COALESCE(NULLIF(TRIM(c.name), ''), c.email,\n"
    "                 'customer #' || c.id::text) AS display_name\n"
    "  Always alias the combined column with a clear name.\n"
    "\n"
    "Top-N defaults — IMPORTANT:\n"
    "  When the user asks 'who is the X' / 'kim eng X' (singular),\n"
    "  RETURN THE TOP 5–10 ROWS, not just LIMIT 1. A small leaderboard\n"
    "  is more useful than a single row, and the UI can highlight #1.\n"
    "  Only use LIMIT 1 when the user explicitly asks for one record\n"
    "  ('show me the single most …', 'just the top one').\n"
    "\n"
    "Richer columns — IMPORTANT:\n"
    "  When ranking entities (users / customers / products), include\n"
    "  the primary metric the user asked for PLUS 1–2 related context\n"
    "  metrics from the same table if they exist (e.g. when ranking\n"
    "  quiz users by session count, also SUM correct_answers and\n"
    "  total_questions, MAX(created_at) AS last_activity). This makes\n"
    "  the table/chart self-explanatory without follow-up questions.\n"
    "  Stay within columns that exist in the schema.\n"
    "\n"
    "Translating natural-language modifiers (English + Uzbek):\n"
    "  * 'most active', 'top user', 'eng faol', 'eng ko\\'p ...'\n"
    "    → AGGREGATE: COUNT/SUM grouped by the relevant id, then\n"
    "      ORDER BY <agg> DESC LIMIT 10 (or LIMIT 5).\n"
    "  * 'latest', 'recent', 'oxirgi', 'so\\'nggi'\n"
    "    → ORDER BY <timestamp> DESC LIMIT N. No GROUP BY needed.\n"
    "  * 'how many', 'qancha', 'nechta' → COUNT(*) without LIMIT 1.\n"
    "  * 'average', 'avg', 'o\\'rtacha' → AVG().\n"
    "  * 'distribution', 'tarqalish', 'ulush' → GROUP BY + share\n"
    "    (counts can be used for proportions).\n"
    "\n"
    "Example — 'Eng faol foydalanuvchi kim?' over\n"
    "  users(id, full_name, username) and\n"
    "  quiz_sessions(user_id, created_at, total_questions, correct_answers):\n"
    "    SELECT\n"
    "      COALESCE(NULLIF(TRIM(u.full_name), ''), u.username,\n"
    "               'user #' || u.id::text) AS display_name,\n"
    "      COUNT(qs.id) AS sessions_count,\n"
    "      SUM(qs.correct_answers) AS correct_total,\n"
    "      SUM(qs.total_questions) AS questions_total,\n"
    "      MAX(qs.created_at) AS last_activity\n"
    "    FROM users u JOIN quiz_sessions qs ON u.id = qs.user_id\n"
    "    GROUP BY u.id, u.full_name, u.username\n"
    "    ORDER BY sessions_count DESC\n"
    "    LIMIT 10;\n"
    "  (NOT: ORDER BY qs.created_at DESC LIMIT 1, NOT: only full_name,\n"
    "  NOT: LIMIT 1 — show a leaderboard.)"
)

_ES_SYSTEM = (
    "You are an Elasticsearch query planner for a strict READ-ONLY "
    "analytics tool. Generate exactly ONE search request that answers "
    "the user's question against the provided index mapping.\n"
    "\n"
    "Output contract — IMPORTANT:\n"
    "  The `sql` field of your SqlPlan must be a JSON ENVELOPE STRING "
    "with this shape:\n"
    '    {"index": "<index pattern>", "body": { ... ES request body ... }}\n'
    "  The `dialect` field must be exactly \"elasticsearch\".\n"
    "  Because `sql` is a JSON string field, every double quote inside "
    "your envelope must be backslash-escaped — your final SqlPlan JSON "
    "will contain the envelope as a quoted string.\n"
    "\n"
    "Rules:\n"
    "  * SEARCH ONLY. Never include `script`, `script_score`, "
    "`script_fields`, `scripted_metric`, `runtime_mappings` with "
    "scripts, `_delete_by_query`, `_update_by_query`, or `_reindex`.\n"
    "  * Reference ONLY indices and fields from the schema.\n"
    "  * If the user asks for a COUNT or AGGREGATE, set body.size to 0 "
    "and use body.aggs.\n"
    "  * If the user wants raw documents, keep body.size <= 50 and use "
    "body.sort to order them.\n"
    "  * Use keyword sub-fields for term aggregations on text fields: "
    "if the mapping shows `name` as text, prefer `name.keyword`.\n"
    "  * NEVER use the name `doc_count` for a sub-aggregation — that "
    "overrides the bucket's automatic doc count and confuses charts.\n"
    "\n"
    "Time-window parsing — IMPORTANT:\n"
    "  Read the user's NATURAL-LANGUAGE date phrase and translate it "
    "into a body.query.bool.filter.range clause. Do NOT skip the filter "
    "when the user mentioned a window. The most common cases:\n"
    "    'uch oylik' / 'oxirgi 3 oy' / 'last 3 months' / 'past quarter' "
    "→ {\"range\":{\"<date_field>\":{\"gte\":\"now-3M/d\"}}}\n"
    "    'oxirgi 30 kun' / 'last 30 days'                       "
    "→ {\"range\":{\"<date_field>\":{\"gte\":\"now-30d/d\"}}}\n"
    "    'shu hafta' / 'this week'                              "
    "→ {\"range\":{\"<date_field>\":{\"gte\":\"now/w\"}}}\n"
    "    'shu oy' / 'this month'                                "
    "→ {\"range\":{\"<date_field>\":{\"gte\":\"now/M\"}}}\n"
    "    'shu yil' / 'this year' / 'yil davomida'               "
    "→ {\"range\":{\"<date_field>\":{\"gte\":\"now/y\"}}}\n"
    "    '2024 yil' / 'in 2024'                                 "
    "→ {\"range\":{\"<date_field>\":{\"gte\":\"2024-01-01\","
    "\"lt\":\"2025-01-01\"}}}\n"
    "  Pick the most plausible date field from the schema (`created_at`, "
    "`@timestamp`, `ordered_at`, `event_time`, etc.). If the user gave "
    "NO time hint, omit the range filter.\n"
    "\n"
    "Common aggregation patterns:\n"
    "  * 'how many X' → size=0, aggs that bucket / count.\n"
    "  * 'top N by Y' → aggs.terms on the group field with size=N, a "
    "metric sub-agg (sum / avg) on Y, ORDER BY the metric DESC by "
    "setting terms.order = {\"<metric_name>\": \"desc\"}.\n"
    "  * 'trend over time' → aggs.date_histogram on the @timestamp / "
    "date field with calendar_interval set to day/week/month.\n"
    "  * 'filter X by Y' → bool.filter clauses (term / range / match).\n"
    "\n"
    "Example — 'top 5 customers by total revenue this month' over "
    "index `orders` with fields customer.keyword, amount, ordered_at:\n"
    '    {"index":"orders","body":{"size":0,"query":{"bool":{"filter":'
    '[{"range":{"ordered_at":{"gte":"now/M"}}}]}},"aggs":{"by_customer":'
    '{"terms":{"field":"customer.keyword","size":5,"order":{"total":'
    '"desc"}},"aggs":{"total":{"sum":{"field":"amount"}}}}}}}\n'
    "\n"
    "Example — 'uch oylik savdoni grafik korinishda korsat' over index "
    "`_all` with `created_at` and `metadata.revenue_usd`:\n"
    '    {"index":"_all","body":{"size":0,"query":{"bool":{"filter":'
    '[{"range":{"created_at":{"gte":"now-3M/d"}}}]}},"aggs":'
    '{"revenue_trend":{"date_histogram":{"field":"created_at",'
    '"calendar_interval":"month"},"aggs":{"total_revenue":{"sum":'
    '{"field":"metadata.revenue_usd"}}}}}}}\n'
    "  (Note the range filter — without it the planner returns the "
    "entire history instead of the requested 3 months.)\n"
    "\n"
    "Plan ONE request only. If the schema cannot answer the question, "
    "write the closest meaningful aggregation and explain in the "
    "rationale."
)

_MONGO_SYSTEM = (
    "You are a MongoDB query planner for a strict READ-ONLY analytics "
    "tool. Generate exactly ONE aggregation pipeline against a single "
    "collection that answers the user's question.\n"
    "\n"
    "Output contract — IMPORTANT:\n"
    "  The `sql` field of your SqlPlan must be a JSON ENVELOPE STRING "
    "with this shape:\n"
    '    {"database": "<db>", "collection": "<coll>", "pipeline": [...]}\n'
    "  The `dialect` field must be exactly \"mongodb\".\n"
    "  Because `sql` is a JSON string field, every double quote inside "
    "your envelope must be backslash-escaped — your final SqlPlan JSON "
    "will contain the envelope as a quoted string.\n"
    "\n"
    "Rules — HARD REJECTS (the validator will refuse the plan):\n"
    "  * NEVER include $out or $merge — those are write stages.\n"
    "  * NEVER include $function, $accumulator, or $where — those run "
    "arbitrary JavaScript and are banned wholesale.\n"
    "  * NEVER touch the 'admin', 'config', or 'local' databases or "
    "any collection starting with 'system.'.\n"
    "  * Allowed pipeline stages: $match, $group, $project, $sort, "
    "$limit, $skip, $count, $unwind, $lookup (NO sub-pipeline writes), "
    "$facet, $bucket, $bucketAuto, $addFields, $set, $replaceRoot, "
    "$replaceWith, $sortByCount, $redact, $densify, $fill. Any other "
    "stage is rejected.\n"
    "  * Reference ONLY collections and fields that appear in the schema.\n"
    "\n"
    "Time-window parsing — same as for SQL/ES. Translate natural-language "
    "phrases into a $match stage using BSON extended JSON for dates:\n"
    "    'uch oylik' / 'last 3 months' →\n"
    '       {"$match":{"<date_field>":{"$gte":{"$date":"<ISO 3 months ago>"}}}}\n'
    "    'shu oy' / 'this month' →\n"
    '       {"$match":{"<date_field>":{"$gte":{"$date":"<start of month ISO>"}}}}\n'
    "  Pick the most plausible date field from the schema (created_at, "
    "createdAt, timestamp, etc.). If the user gave NO time hint, omit "
    "the filter.\n"
    "\n"
    "Aggregation patterns:\n"
    "  * 'how many X' → [{\"$match\":...}, {\"$count\":\"count\"}].\n"
    "  * 'top N by Y' →\n"
    '       [{"$group":{"_id":"$<group_field>","total":{"$sum":"$<Y>"}}},\n'
    '        {"$sort":{"total":-1}}, {"$limit": N}].\n'
    "  * 'trend over time' → $group on a date trunc via $dateTrunc:\n"
    '       {"$group":{"_id":{"$dateTrunc":{"date":"$created_at","unit":"month"}},'
    '"total":{"$sum":"$amount"}}}\n'
    "    then $sort: {_id: 1}.\n"
    "\n"
    "Example — 'top 5 customers by total revenue this month' over\n"
    "  database 'shop', collection 'orders' with fields customer, amount, "
    "ordered_at:\n"
    '    {"database":"shop","collection":"orders","pipeline":'
    '[{"$match":{"ordered_at":{"$gte":{"$date":"2026-05-01T00:00:00Z"}}}},'
    '{"$group":{"_id":"$customer","total":{"$sum":"$amount"}}},'
    '{"$sort":{"total":-1}},{"$limit":5}]}\n'
    "\n"
    "Plan ONE pipeline only. If the schema cannot answer the question, "
    "write the closest meaningful aggregation and explain in the rationale."
)

_API_SYSTEM = (
    "You are a REST API query planner for a strict READ-ONLY analytics "
    "tool. Generate exactly ONE GET request against the API catalog "
    "below that answers the user's question.\n"
    "\n"
    "Output contract — IMPORTANT:\n"
    "  The `sql` field of your SqlPlan must be a JSON ENVELOPE STRING "
    "with this shape:\n"
    '    {"endpoint":"/path","method":"GET",'
    '"path_params":{},"query_params":{},"headers":{},'
    '"json_path":"$.data","row_field_paths":{}}\n'
    "  The `dialect` field must be exactly \"rest_api\". `method` must "
    "be \"GET\". Because `sql` is a JSON string field, every double "
    "quote inside your envelope must be backslash-escaped.\n"
    "\n"
    "Catalog mapping:\n"
    "  Each row in the schema below corresponds to ONE callable "
    "endpoint. The table name is encoded as 'GET <path>'. Columns "
    "whose data_type starts with 'param:' are query/path parameters "
    "(name prefixed with '@'); columns without that prefix are fields "
    "you can expect to find in the response body.\n"
    "\n"
    "Rules:\n"
    "  * GET ONLY. Never set method to POST/PUT/PATCH/DELETE.\n"
    "  * endpoint MUST exactly match one of the catalog's 'GET <path>' "
    "rows. For templates like '/users/{id}', fill {id} in path_params; "
    "do not write the literal '{id}' in the endpoint string.\n"
    "  * Do NOT invent endpoints, params, or response fields not in the "
    "schema.\n"
    "  * Translate the user's question into query_params using the "
    "param names from the catalog. Common patterns:\n"
    "      'first 50 X'      → query_params['limit']=50 or "
    "['$top']=50 (1C OData) or ['start']=0 (Bitrix24).\n"
    "      'this month'      → use the API's date filter param if one "
    "exists in the catalog (e.g. filter[created_at][from] in AmoCRM, "
    "or $filter for OData with ge/le clauses).\n"
    "      'top N by Y'      → most REST APIs don't sort server-side; "
    "request a reasonable page size and the answer node will sort.\n"
    "  * Set json_path if the catalog hints at a wrapper field "
    "(Bitrix → '$.result', HubSpot → '$.results', OData → '$.value'). "
    "Otherwise omit — the engine probes common shapes automatically.\n"
    "\n"
    "1C OData specifics:\n"
    "  * Always include query_params['$format']='json' so the engine "
    "gets JSON not Atom XML.\n"
    "  * Use $filter with ge/le/eq operators: e.g. "
    "$filter=Date ge datetime'2026-01-01T00:00:00'.\n"
    "  * $top caps row count; $skip paginates.\n"
    "\n"
    "Bitrix24 specifics:\n"
    "  * Pagination uses 'start' (0-based row offset, page size 50).\n"
    "  * Filters use 'filter[FIELD]=VALUE'. Multiple selects use "
    "select[]=ID&select[]=NAME.\n"
    "  * Response is wrapped {'result':[...], 'next':50, 'total':123}; "
    "set json_path='$.result'.\n"
    "\n"
    "AmoCRM specifics:\n"
    "  * Date filters use UNIX seconds: filter[created_at][from]=...\n"
    "  * Response is wrapped {'_embedded':{'leads':[...]}, '_links':...};"
    " set json_path='$._embedded.leads' (substitute leads/contacts/etc).\n"
    "\n"
    "HubSpot specifics:\n"
    "  * 'limit' parameter; 'after' for cursor pagination.\n"
    "  * Response: {'results':[...], 'paging':{...}}; json_path='$.results'.\n"
    "\n"
    "Plan ONE request. If the schema can't satisfy the question, pick "
    "the closest endpoint and explain in the rationale."
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

    # Dispatch the prompt by connection query language. SQL engines all
    # share the SqlPlan output shape ``{sql, dialect, rationale, …}``;
    # Elasticsearch reuses the same shape but the `sql` field holds a
    # JSON envelope string (see _ES_SYSTEM).
    # Dispatch by connection language. SQL dialects share the same
    # planner prompt; ES and MongoDB each have their own because the
    # output language (JSON envelopes vs SQL) differs.
    dialect = bundle.dialect if bundle is not None else None
    if dialect == "elasticsearch":
        system_prompt = _ES_SYSTEM
    elif dialect == "mongodb":
        system_prompt = _MONGO_SYSTEM
    elif dialect == "rest_api":
        system_prompt = _API_SYSTEM
    else:
        system_prompt = _SQL_SYSTEM

    prompt_user = (
        f"Question: {state.get('user_message','')}\n\n"
        f"Schema:\n{_schema_brief(bundle, keep)}\n\n"
        + (f"Reference context:\n{rag_extras}\n\n" if rag_extras else "")
        + ("\n".join(feedback) + "\n\n" if feedback else "")
        + "Return a SqlPlan."
    )

    # Inject recent turns so the planner can resolve follow-ups like
    # "show as chart" against the previous user question.
    history = state.get("conversation_history") or []
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system_prompt}
    ]
    for h in history[-6:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if role not in ("user", "assistant") or not content:
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": prompt_user})

    llm = get_llm()
    try:
        plan = await llm.structured(messages, SqlPlan)
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
