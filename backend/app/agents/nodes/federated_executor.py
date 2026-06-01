"""Run a FederatedPlan: validate each sub-query, execute them in
parallel, then fold the results through the merge pipeline.

This node is the federated counterpart of ``query_executor``. It
collapses three responsibilities into one node because, unlike the
single-DB path, there's no sensible mid-point to split:

  1. **Validate** each SubQuery via its connection's engine
     (``engine.validate_readonly``). SQL engines run the sqlglot AST
     walker; the ES engine runs the JSON-DSL validator. One failure
     aborts the entire plan.

  2. **Execute** each SubQuery in parallel via ``asyncio.gather``.
     Each gets the connection's decrypted credentials, constructs the
     engine, calls ``execute(query)``, and closes the engine.

  3. **Merge** through the plan's ordered merge_steps via
     :func:`services.federation_merge.execute_merge_pipeline`. The
     final ResultSet lands on ``state.result`` so chart_designer +
     answer_writer keep working unchanged.

Errors at any step land on ``last_executor_error`` so the existing
retry edge can route back to the planner. After
``MAX_EXECUTOR_ATTEMPTS`` failures the graph escalates to
``error_responder``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.state import GraphState
from app.config import settings
from app.db.models import WorkspaceConnection, WorkspaceCredentials
from app.engines import register_all as register_engines
from app.engines.base import ResultSet
from app.engines.registry import get_engine
from app.services import crypto
from app.services.api_query_validator import validate_api_query
from app.services.es_readonly_validator import validate_es_query
from app.services.graphql_readonly_validator import validate_graphql_query
from app.services.mongo_readonly_validator import validate_mongo_query
from app.services.federation_merge import MergeError, execute_merge_pipeline
from app.services.readonly_validator import validate_readonly

log = logging.getLogger(__name__)


async def _decrypt_creds(
    session, connection_id: UUID
) -> dict[str, str]:
    row = (
        await session.execute(
            select(WorkspaceCredentials).where(
                WorkspaceCredentials.connection_id == connection_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return {}
    # Phase 1 migration didn't re-encrypt — try both AADs.
    conn = await session.get(WorkspaceConnection, connection_id)
    aads: list[bytes | None] = [str(connection_id).encode()]
    if conn is not None:
        aads.append(str(conn.workspace_id).encode())
    raw = crypto.decrypt_with_aads(
        row.ciphertext,
        row.nonce,
        key_version=row.key_version,
        aads=aads,
    )
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {"password": raw.decode("utf-8", errors="replace")}


async def _run_one(
    sub: dict[str, Any],
    conn_row: WorkspaceConnection,
    creds: dict[str, str],
) -> tuple[str, ResultSet]:
    """Validate + execute one SubQuery; return (alias, ResultSet)."""
    dialect = sub["dialect"]
    query = sub["query"]

    # Dialect-dispatched validation. SQL uses sqlglot; ES/Mongo use their
    # JSON-DSL validators; rest_api uses the GET-envelope validator.
    if dialect == "elasticsearch":
        result, _ = validate_es_query(query)
    elif dialect == "mongodb":
        result, _ = validate_mongo_query(query)
    elif dialect == "rest_api":
        result, _ = validate_api_query(query)
    elif dialect == "graphql":
        result, _ = validate_graphql_query(query)
    else:
        result = validate_readonly(query, dialect=dialect)
    if not result.ok:
        codes = ", ".join(f.code for f in result.findings) or "unknown"
        raise RuntimeError(
            f"sub_query alias={sub['alias']!r} failed validation: "
            f"{codes}: " + "; ".join(f.message for f in result.findings[:3])
        )

    sql_to_run = result.rewritten_sql or query
    conn_row._credentials = creds  # type: ignore[attr-defined]
    engine = get_engine(conn_row)
    try:
        rs = await engine.execute(sql_to_run)
    finally:
        await engine.aclose()
    return sub["alias"], rs


async def run(state: GraphState) -> GraphState:
    attempts = int(state.get("executor_attempts", 0)) + 1
    plan: dict[str, Any] | None = state.get("federated_plan")
    if not plan:
        return {
            "executor_attempts": attempts,
            "last_executor_error": "federated_executor invoked without a plan",
        }

    sub_queries: list[dict[str, Any]] = plan.get("sub_queries") or []
    merge_steps: list[dict[str, Any]] = plan.get("merge_steps") or []
    if not sub_queries:
        return {
            "executor_attempts": attempts,
            "last_executor_error": "federated plan has no sub_queries",
        }

    register_engines()

    # Load every referenced connection (and its credentials) up front so
    # the parallel gather doesn't open three sessions to the metadata DB.
    conn_ids = list({UUID(sq["connection_id"]) for sq in sub_queries})
    sa_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    conn_rows: dict[str, WorkspaceConnection] = {}
    creds_by_id: dict[str, dict[str, str]] = {}
    try:
        async with Session() as session:
            rows = await session.execute(
                select(WorkspaceConnection).where(
                    WorkspaceConnection.id.in_(conn_ids)
                )
            )
            for c in rows.scalars().all():
                conn_rows[str(c.id)] = c
                creds_by_id[str(c.id)] = await _decrypt_creds(session, c.id)
    finally:
        await sa_engine.dispose()

    # Make sure every plan id resolved.
    missing = [
        sq["alias"]
        for sq in sub_queries
        if sq["connection_id"] not in conn_rows
    ]
    if missing:
        return {
            "executor_attempts": attempts,
            "last_executor_error": (
                "sub_queries reference connections that no longer exist: "
                + ", ".join(missing)
            ),
        }

    # Run all sub-queries concurrently. ``return_exceptions`` so one bad
    # leg doesn't take the others down before we can report.
    coros = [
        _run_one(
            sq,
            conn_rows[sq["connection_id"]],
            creds_by_id[sq["connection_id"]],
        )
        for sq in sub_queries
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    sub_results: dict[str, ResultSet] = {}
    errors: list[str] = []
    for sq, outcome in zip(sub_queries, results):
        if isinstance(outcome, Exception):
            errors.append(f"{sq['alias']}: {outcome}")
            continue
        alias, rs = outcome  # type: ignore[misc]
        sub_results[alias] = rs

    if errors:
        return {
            "executor_attempts": attempts,
            "last_executor_error": " | ".join(errors),
        }

    # Fold through merge_steps. An empty merge_steps list with a single
    # sub_query is allowed (the sole result is the answer).
    try:
        merged = execute_merge_pipeline(sub_results, merge_steps)
        final_rs = _apply_row_cap(merged, settings.FEDERATION_MAX_ROWS)
    except MergeError as e:
        log.warning("federated_executor: merge failed: %s", e)
        return {
            "executor_attempts": attempts,
            "last_executor_error": f"merge failed: {e}",
        }
    except Exception as e:
        log.exception("federated_executor: unexpected merge crash")
        return {
            "executor_attempts": attempts,
            "last_executor_error": f"merge crashed: {e}",
        }

    # Surface a single executed-SQL string for the audit row. We
    # concatenate sub-queries with a delimiter so query_history retains
    # the full federated context.
    sql_summary = "\n-- next sub-query --\n".join(
        f"-- alias={sq['alias']} dialect={sq['dialect']} "
        f"conn={sq['connection_id']}\n{sq['query']}"
        for sq in sub_queries
    )

    return {
        "result": final_rs,
        "sql_executed": sql_summary,
        "sub_results": {
            alias: {
                "columns": rs.columns,
                "row_count": rs.row_count,
            }
            for alias, rs in sub_results.items()
        },
        "executor_attempts": attempts,
        "last_executor_error": None,
    }


def _apply_row_cap(rs: ResultSet, cap: int) -> ResultSet:
    """Truncate the merged result when a cartesian-style join blows up.

    Each sub-query is already capped at its engine's row_cap (default
    1000), but a join can multiply rows by orders of magnitude. Keep
    the agent's downstream consumers (chart_designer, answer_writer)
    on a bounded payload — the user can refine the question if the
    truncation hides relevant rows.
    """
    if cap <= 0 or rs.row_count <= cap:
        return rs
    capped_rows = rs.rows[:cap]
    return ResultSet(
        columns=rs.columns,
        dtypes=rs.dtypes,
        rows=capped_rows,
        row_count=len(capped_rows),
        truncated=True,
        took_ms=rs.took_ms,
    )
