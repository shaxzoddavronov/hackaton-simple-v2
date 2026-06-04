from __future__ import annotations

from app.agents.state import GraphState
from app.config import settings
from app.services.api_query_validator import validate_api_query
from app.services.es_readonly_validator import validate_es_query
from app.services.graphql_readonly_validator import validate_graphql_query
from app.services.mongo_readonly_validator import validate_mongo_query
from app.services.readonly_validator import validate_readonly
from app.services.row_budget_validator import (
    DEFAULT_MAX_PREDICTED_ROWS,
    validate_row_budget,
)


async def run(state: GraphState) -> GraphState:
    plan = state.get("plan")
    if plan is None:
        return {"last_validation_error": "no plan to validate"}

    # Dispatch by dialect. SQL engines all use the sqlglot AST walker;
    # Elasticsearch uses the JSON-DSL validator (rejects scripts /
    # mutation endpoints / system indices); MongoDB uses the
    # aggregation-pipeline validator (rejects $out/$merge/$function/
    # scripts / system collections); rest_api uses the GET-only
    # envelope validator (rejects POST/PUT/PATCH/DELETE and endpoints
    # not in the introspected catalog).
    if plan.dialect == "elasticsearch":
        result, _envelope = validate_es_query(plan.sql)
    elif plan.dialect == "mongodb":
        result, _envelope = validate_mongo_query(plan.sql)
    elif plan.dialect == "rest_api":
        result, _envelope = validate_api_query(
            plan.sql, schema_bundle=state.get("schema_bundle")
        )
    elif plan.dialect == "graphql":
        result, _envelope = validate_graphql_query(plan.sql)
    else:
        result = validate_readonly(plan.sql, dialect=plan.dialect)

    # Phase 41 — row-budget guard. Runs only when the read-only /
    # DSL check passed; rejecting on budget would otherwise mask
    # the more critical security finding. Advisory by default: no
    # schema bundle / no row_count_estimate → pass.
    if result.ok:
        cap = getattr(
            settings, "MAX_PREDICTED_ROWS", DEFAULT_MAX_PREDICTED_ROWS
        )
        budget = validate_row_budget(
            plan.sql,
            dialect=plan.dialect,
            schema_bundle=state.get("schema_bundle"),
            max_predicted_rows=cap,
        )
        if not budget.ok:
            result = budget

    out: GraphState = {"validation": result}
    if not result.ok:
        codes = ", ".join(f.code for f in result.findings) or "unknown"
        out["last_validation_error"] = f"{codes}: " + "; ".join(f.message for f in result.findings[:3])
    return out
