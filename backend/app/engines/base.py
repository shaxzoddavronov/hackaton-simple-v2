from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# SQL-style dialects (sqlglot-validated, JDBC-shaped).
SqlDialect = Literal["postgres", "sqlite", "mysql", "clickhouse", "oracle"]
# Non-SQL dialects (Mongo aggregation pipelines, ES Query DSL). Their
# engines will be plugged in via the same registry but speak different
# query languages — readonly_validator does not apply to them.
NoSqlDialect = Literal["mongodb", "elasticsearch"]
Dialect = Literal[
    "postgres",
    "sqlite",
    "mysql",
    "clickhouse",
    "oracle",
    "mongodb",
    "elasticsearch",
]


class ColumnMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: str
    nullable: bool
    is_pk: bool = False
    is_unique: bool = False
    is_id: bool = False
    fk_to: str | None = None


class ColumnSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    distinct_values: list[Any] | None = None
    distinct_truncated: bool = False
    numeric_stats: dict[str, float] | None = None
    sample_rows: list[Any] | None = None
    non_null_count: int | None = None


class ForeignKeyMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_columns: list[str]
    to_table: str
    to_columns: list[str]


class TableMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema: str
    name: str
    columns: list[ColumnMeta]
    foreign_keys: list[ForeignKeyMeta] = Field(default_factory=list)
    row_count_estimate: int | None = None


class SchemaBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: Dialect
    tables: list[TableMeta]
    samples: dict[str, dict[str, ColumnSample]] = Field(default_factory=dict)


class ResultSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    dtypes: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    took_ms: int


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    node_kind: str | None = None


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    rewritten_sql: str | None = None
    findings: list[ValidationFinding] = Field(default_factory=list)


@runtime_checkable
class QueryEngine(Protocol):
    dialect: Dialect

    async def introspect_schema(self) -> SchemaBundle: ...

    async def sample_column(self, table: TableMeta, col: ColumnMeta) -> ColumnSample: ...

    def validate_readonly(self, sql: str) -> ValidationResult: ...

    async def execute(
        self, sql: str, *, row_cap: int = 1000, timeout_s: int = 10
    ) -> ResultSet: ...

    async def aclose(self) -> None: ...
