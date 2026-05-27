"""LLM-driven planner for cross-connection queries.

Given multiple schema bundles (one per workspace connection), this
node asks the model to emit a :class:`FederatedPlan`: N sub-queries
(one per relevant DB) plus an ordered list of merge steps that fold
them into a single result.

The prompt explicitly lists each connection's UUID, name, and
dialect so the model can reference them by ID. Schema snippets per
connection are condensed for prompt budget — we feed only the
table+column listing (no samples) for non-target connections, and
samples for the connection most relevant to the user's question (TBD;
for now we send samples for ALL since the LLM may need them).

If the LLM produces invalid JSON the existing planner_attempts retry
loop catches it (we reuse those counters so federation gets the same
two-strike budget as single-DB planning).
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.agents.llm import get_llm
from app.agents.state import GraphState
from app.config import settings
from app.engines.base import SchemaBundle
from app.schemas.llm_io import FederatedPlan
from app.services.schema_pruner import prune

log = logging.getLogger(__name__)


_SYSTEM = (
    "You are a FEDERATED query planner for a strict READ-ONLY analytics "
    "tool. The user's question requires data from TWO OR MORE database "
    "connections inside one workspace. Decompose the question into:\n"
    "  1) One SubQuery per connection that needs to run a query.\n"
    "  2) A sequence of MergeStep operations that fold the sub-results "
    "into a single table.\n"
    "\n"
    "Each SubQuery has:\n"
    "  * connection_id — the UUID from the list below.\n"
    "  * dialect — one of postgres/sqlite/mysql/clickhouse/oracle/"
    "elasticsearch (must match the connection).\n"
    "  * query — for SQL dialects: a single SELECT, no DML/DDL, only "
    "tables/columns that appear in that connection's schema. For "
    "elasticsearch: a JSON envelope string {\"index\":\"...\","
    "\"body\":{...}}. Apply the same safety rules as the single-DB "
    "planner (no scripts, no system tables, no INTERVAL filters the "
    "user didn't ask for).\n"
    "  * alias — short snake_case nickname for this sub-result.\n"
    "  * rationale — 1 sentence about what this sub-query returns.\n"
    "\n"
    "Each MergeStep has:\n"
    "  * kind — 'join' (inner equijoin), 'union' (vertical stack with "
    "dedup, identical columns required), or 'concat' (vertical stack "
    "with column union, no dedup).\n"
    "  * left, right — either a SubQuery.alias or an earlier "
    "MergeStep.output.\n"
    "  * on — list of join columns (required for kind='join', empty "
    "for union/concat).\n"
    "  * output — alias of the merged result. The LAST merge step's "
    "output becomes the user-visible answer.\n"
    "\n"
    "Rules for joins across heterogeneous stores:\n"
    "  * SELECT the join key in BOTH sub-queries (e.g., both sides must "
    "project `user_id`). Make sure the column names match.\n"
    "  * Cast/alias columns so the merge sees identical types and names.\n"
    "  * Keep sub-queries focused — only project what merge_steps need.\n"
    "  * If one sub-query is enough, return a single SubQuery with NO "
    "merge_steps (federated_query routed here by mistake).\n"
    "\n"
    "Example — workspace has two connections:\n"
    "  pg-orders  (postgres) — tables: orders(user_id, amount, ts)\n"
    "  mongo-users (mongodb)  — collections: users(user_id, full_name, "
    "country)\n"
    "User asks: 'top 5 countries by total revenue this month'\n"
    "Output:\n"
    "  sub_queries:\n"
    "    - alias: orders_by_user, connection_id: <pg uuid>, "
    "dialect: postgres,\n"
    "      query: 'SELECT user_id, SUM(amount) AS revenue FROM orders "
    "WHERE ts >= date_trunc(''month'', now()) GROUP BY user_id'\n"
    "    - alias: users_country, connection_id: <mongo uuid>, "
    "dialect: mongodb,\n"
    "      query: '...' (Mongo aggregation pipeline JSON)\n"
    "  merge_steps:\n"
    "    - kind: join, left: orders_by_user, right: users_country, "
    "on: ['user_id'], output: joined\n"
    "  (Then in a real plan we'd add an extra aggregation step — but "
    "for now keep merge_steps simple and let the answer node summarize.)"
)


def _brief_for(
    connection_id: str,
    bundle: SchemaBundle,
    name: str,
    *,
    user_question: str,
    top_k: int,
) -> str:
    """One connection's schema condensed for the planner prompt.

    We BM25-prune each bundle independently so the prompt stays
    bounded regardless of how many tables the connection has. Tables
    whose name literally appears in the user's question are pinned
    (see ``services.schema_pruner.prune``).

    When a bundle has at most ``top_k`` tables we skip pruning to
    save the work — and to preserve full visibility on tiny DBs.
    """
    if len(bundle.tables) <= top_k:
        kept_tables = list(bundle.tables)
        truncated = False
    else:
        pruned = prune(bundle, user_question, top_k=top_k)
        kept_qnames = set(pruned.selected_tables)
        kept_tables = [
            t for t in bundle.tables if f"{t.schema}.{t.name}" in kept_qnames
        ]
        truncated = True

    header = (
        f"=== Connection {name} (id={connection_id}, "
        f"dialect={bundle.dialect}, tables_shown={len(kept_tables)}"
        f"/{len(bundle.tables)}) ==="
    )
    parts: list[str] = [header]
    for t in kept_tables:
        qn = f"{t.schema}.{t.name}"
        cols = ", ".join(f"{c.name}:{c.data_type}" for c in t.columns)
        line = f"  - {qn}({cols})"
        if t.foreign_keys:
            fks = "; ".join(
                f"{','.join(fk.from_columns)}->{fk.to_table}({','.join(fk.to_columns)})"
                for fk in t.foreign_keys
            )
            line += f"  fks: {fks}"
        parts.append(line)
    if truncated:
        parts.append(
            f"  (… {len(bundle.tables) - len(kept_tables)} more tables "
            "omitted; ask about them by name if you need them.)"
        )
    return "\n".join(parts)


async def run(state: GraphState) -> GraphState:
    attempts = int(state.get("planner_attempts", 0)) + 1
    bundles: dict[str, SchemaBundle] = state.get("connection_bundles") or {}
    if not bundles:
        return {
            "planner_attempts": attempts,
            "federated_plan": None,
            "last_validation_error": (
                "federated_planner invoked without any connection_bundles"
            ),
        }

    # Look up the connection name for each id so the prompt is readable.
    # We don't have direct access here; the bundles dict only carries
    # ids. Names live on WorkspaceConnection — pull them lazily.
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.db.models import WorkspaceConnection
    from uuid import UUID

    names: dict[str, str] = {}
    sa_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    try:
        async with Session() as session:
            ids = [UUID(k) for k in bundles.keys()]
            rows = await session.execute(
                select(WorkspaceConnection).where(WorkspaceConnection.id.in_(ids))
            )
            for c in rows.scalars().all():
                names[str(c.id)] = c.name
    finally:
        await sa_engine.dispose()

    user_question = state.get("user_message", "")
    top_k = max(1, int(settings.FEDERATED_TOP_K))
    blocks = [
        _brief_for(
            cid,
            b,
            names.get(cid, "?"),
            user_question=user_question,
            top_k=top_k,
        )
        for cid, b in bundles.items()
    ]
    schema_block = "\n\n".join(blocks)

    feedback: list[str] = []
    if state.get("last_validation_error"):
        feedback.append(
            f"Previous attempt rejected: {state['last_validation_error']}"
        )

    prompt_user = (
        f"Question: {state.get('user_message','')}\n\n"
        f"{schema_block}\n\n"
        + ("\n".join(feedback) + "\n\n" if feedback else "")
        + "Return a FederatedPlan."
    )

    llm = get_llm()
    try:
        plan = await llm.structured(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt_user},
            ],
            FederatedPlan,
        )
    except ValidationError as e:
        err_summary = "; ".join(
            f"{'.'.join(str(x) for x in (it.get('loc') or []))}: {it.get('msg')}"
            for it in e.errors()[:3]
        ) or str(e)[:300]
        log.warning("federated_planner: schema validation failed: %s", err_summary)
        return {
            "planner_attempts": attempts,
            "federated_plan": None,
            "last_validation_error": (
                "LLM returned JSON that doesn't match FederatedPlan. "
                f"Errors: {err_summary}. Return a valid plan with "
                "sub_queries[] and merge_steps[]."
            ),
        }

    # Cross-validate: each SubQuery.connection_id must exist in bundles
    # and dialects must match.
    issues: list[str] = []
    for sq in plan.sub_queries:
        if sq.connection_id not in bundles:
            issues.append(
                f"sub_query alias={sq.alias} references unknown "
                f"connection_id={sq.connection_id}"
            )
            continue
        actual_dialect = bundles[sq.connection_id].dialect
        if sq.dialect != actual_dialect:
            issues.append(
                f"sub_query alias={sq.alias} declared dialect={sq.dialect} "
                f"but connection is {actual_dialect}"
            )
    # Alias uniqueness
    aliases = [sq.alias for sq in plan.sub_queries]
    if len(set(aliases)) != len(aliases):
        issues.append("sub_query aliases must be unique")
    # Merge step references
    known_aliases = set(aliases)
    for ms in plan.merge_steps:
        if ms.left not in known_aliases:
            issues.append(f"merge step references unknown alias {ms.left!r}")
        if ms.right not in known_aliases:
            issues.append(f"merge step references unknown alias {ms.right!r}")
        if ms.output in known_aliases:
            issues.append(
                f"merge step output {ms.output!r} collides with an existing alias"
            )
        known_aliases.add(ms.output)
        if ms.kind == "join" and not ms.on:
            issues.append(f"merge step output={ms.output} is join but `on` is empty")

    if issues:
        return {
            "planner_attempts": attempts,
            "federated_plan": None,
            "last_validation_error": (
                "Plan cross-validation failed: " + "; ".join(issues)
            ),
        }

    return {
        "planner_attempts": attempts,
        "federated_plan": plan.model_dump(mode="json"),
        "last_validation_error": None,
        "last_executor_error": None,
    }
