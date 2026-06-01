from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "chitchat",
        "metadata",
        "data_query",
        "dashboard",
        "federated_query",
        "clarify",
    ]
    workspace_hint: str | None = Field(
        default=None,
        description="Workspace name extracted from @ mention or bare-word match.",
    )


class SqlPlan(BaseModel):
    """The planner's structured output.

    For SQL dialects the ``sql`` field holds a single SELECT. For
    Elasticsearch the same field holds a JSON envelope string —

        {"index": "...", "body": {"query": ..., "aggs": ...}}

    — that the ES engine parses. The validator dispatches by dialect:
    SQL → sqlglot read-only validator, ES → JSON DSL validator.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: Literal[
        "postgres",
        "sqlite",
        "mysql",
        "clickhouse",
        "oracle",
        "elasticsearch",
        "duckdb",
        "mssql",
        "mongodb",
        "rest_api",
        "snowflake",
        "bigquery",
    ]
    sql: str = Field(
        description=(
            "For SQL dialects: a single read-only SELECT. "
            "For elasticsearch: a JSON envelope "
            '{"index":"...","body":{...}}. '
            "For mongodb: a JSON envelope "
            '{"database":"...","collection":"...","pipeline":[...]}. '
            "For rest_api: a JSON envelope "
            '{"endpoint":"/path","method":"GET","query_params":{...},...}.'
        )
    )
    rationale: str = Field(
        description="One sentence: why this query answers the user's question."
    )
    expected_columns: list[str] = Field(
        description="Column names the planner expects to see in the result."
    )


class SubQuery(BaseModel):
    """One leg of a federated plan — a single query against one DB.

    The ``alias`` is the local name the planner uses to refer to this
    result in :class:`MergeStep`. Aliases must be unique within a plan
    and follow ``[a-z_][a-z0-9_]*``.
    """

    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(
        description="UUID of the WorkspaceConnection this query targets."
    )
    dialect: Literal[
        "postgres", "sqlite", "mysql", "clickhouse",
        "oracle", "elasticsearch", "duckdb", "mssql",
        "mongodb", "rest_api", "snowflake", "bigquery",
    ]
    query: str = Field(
        description=(
            "For SQL dialects: a single read-only SELECT. "
            "For elasticsearch: a JSON envelope "
            '{"index":"...","body":{...}}. '
            "For mongodb: a JSON envelope "
            '{"database":"...","collection":"...","pipeline":[...]}. '
            "For rest_api: a JSON envelope "
            '{"endpoint":"/path","method":"GET","query_params":{...},...}.'
        )
    )
    alias: str = Field(
        description="Local name for this sub-result. Snake_case, unique per plan.",
    )
    rationale: str = Field(
        description="One sentence: what this sub-query returns and why."
    )


class MergeStep(BaseModel):
    """A single merge over two sub-results (or earlier merge outputs).

    ``kind``:
      * ``join``  — inner equijoin on ``on`` columns. Output columns =
        left columns + right columns minus the duplicate join keys.
      * ``union`` — vertical stack with duplicate-row deduplication.
        Both inputs must have identical column sets.
      * ``concat`` — vertical stack with NO dedup. Column union; missing
        cells are NULL.

    ``left`` / ``right`` reference either a sub_query alias or a
    previous merge_step ``output``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["join", "union", "concat"]
    left: str
    right: str
    on: list[str] = Field(
        default_factory=list,
        description="Join keys. Required for 'join', empty for union/concat.",
    )
    output: str = Field(
        description="Alias to bind to the merged result; the LAST step's "
        "output is what the agent returns to the user."
    )


class FederatedPlan(BaseModel):
    """The planner output for cross-connection questions.

    Each sub_query runs independently against its connection; the
    merge_steps then fold the sub-results into a single tabular result.
    The agent uses the **last** merge_step's ``output`` as the final
    table. If a plan has exactly one sub_query and no merges, the
    sub-result IS the answer.
    """

    model_config = ConfigDict(extra="forbid")

    sub_queries: list[SubQuery] = Field(min_length=1, max_length=5)
    merge_steps: list[MergeStep] = Field(default_factory=list, max_length=5)
    rationale: str
    expected_columns: list[str] = Field(default_factory=list)


class KeyNumber(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: float | str
    unit: str | None = None


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(
        description="One-line summary. Used as page title and chat preview."
    )
    body_md: str = Field(
        description="2-4 sentence markdown narrative referencing the result."
    )
    key_numbers: list[KeyNumber] = Field(
        default_factory=list,
        description="Pull-out metrics highlighted in the UI.",
    )
