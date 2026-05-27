# QueryMind AI — Implementation Plan

## Context

**Why this is being built.** QueryMind is an AI-powered database analytics platform where users connect their own databases and ask questions in natural language. The system extracts the database schema deterministically, then an agentic AI generates **read-only** SQL, executes it, and returns a written answer plus an optional chart/dashboard. The platform must be self-hosted and run **only** against a local vLLM server (no external AI APIs) on the user's 2× L40S 48 GB hardware.

**What prompted this plan.** The working tree is empty (prior Elasticsearch-flavored scaffold was deleted; the user chose to start fresh). The new requirement set replaces the ES focus with multi-dialect SQL + MongoDB, introduces an explicit Orchestrator + 4 sub-agent topology, and ships a "Neural Dark" glassmorphism UI per `ui_images/`.

**Confirmed v1 scope (per user decisions in this session):**
- **Dialects:** Postgres + SQLite only. The `QueryEngine` adapter is designed so MySQL / MSSQL / MongoDB plug in later without rewrites.
- **Model:** `google/gemma-3-4b-it` served by vLLM with `xgrammar` guided decoding.
- **Auth:** Multi-user — email/password + JWT, bcrypt, per-user workspace isolation.
- **Code base:** Greenfield. Do not restore prior commit.

---

## Confirmed Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI + Python 3.11, async | Fits agentic + SSE chat streaming; widely modeled |
| ORM | SQLAlchemy 2.0 async | Uniform pool/dialect handling across PG + SQLite (+ future MySQL/MSSQL) |
| Migrations | Alembic | Standard, integrates with SQLAlchemy |
| Agent graph | LangGraph | Native fan-out, retry loops, typed state |
| LLM runtime | vLLM (OpenAI-compatible REST) | Local, supports `xgrammar` JSON-schema guided decoding |
| Model | `google/gemma-3-4b-it` | Fits comfortably on one L40S; reliable JSON mode |
| SQL parser/validator | `sqlglot` | Dialect-aware AST walking; safer than regex |
| Background jobs | Celery + Redis | Schema profiling is async; Redis also brokers SSE |
| App metadata DB | Postgres 16 | Single source of truth for app state |
| Auth | JWT (bcrypt password hash) | Multi-user isolation, simple to demo |
| Frontend | Next.js 14 App Router + React + TailwindCSS + Recharts | Modern, SSE-friendly, design tokens map cleanly to Tailwind |
| Crypto | `cryptography` AES-GCM 256-bit | Industry-standard authenticated encryption for stored DB creds |

---

## System Architecture

**Schema ingestion** (workspace creation, async):

```
[Browser] -- POST /workspaces (creds) --> [FastAPI]
                                              | encrypts creds (AES-GCM), inserts workspace,
                                              | enqueues profile_job (Celery via Redis)
                                              v
                                          [Celery worker]
                                              | constructs QueryEngine for dialect,
                                              | calls introspect_schema() + sample_column()
                                              v
                                          [User DB] (read-only)
                                              | returns rows
                                              v
                                          writes schema_bundles (jsonb) in Postgres
[Browser] <-- SSE job status -- [FastAPI]
```

**Chat / query flow** (SSE-streamed):

```
[Browser] -- POST /chat (workspace_id, message) --> [FastAPI /chat]
                                                          | constructs GraphState
                                                          v
                                                   [LangGraph runner]
   coordinator --> schema_loader --> query_planner --> query_validator --> query_executor
       |               |                  ^                   |                    |
       |  (chitchat)   |                  |    retry <=2      |       retry <=2    |
       v               |                  +-------------------+        ____________|
   answer_writer       |                                              |            |
       |               |                                       chart_designer  answer_writer
       v               v                                              \           /
                  finalizer <--------------------------------------- finalizer
                       |
                       v
                  UISpec JSON streamed (SSE) → frontend RenderSpec
```

LLM-driven nodes: `coordinator`, `query_planner`, `chart_designer`, `answer_writer`. Deterministic: `schema_loader`, `query_validator`, `query_executor`, `finalizer`, `error_responder`.

---

## Multi-dialect via Adapter Pattern

One ABC, registered subclasses, never branched on dialect elsewhere.

```python
# backend/app/engines/base.py
class QueryEngine(Protocol):
    dialect: Literal["postgres","sqlite"]            # v1; "mysql","mssql","mongo" added later
    async def introspect_schema(self) -> SchemaBundle: ...
    async def sample_column(self, table: str, col: ColumnMeta) -> ColumnSample: ...
    def validate_readonly(self, query: str) -> ValidationResult: ...
    async def execute(self, query: str, *, row_cap=1000, timeout_s=10) -> ResultSet: ...
    async def aclose(self) -> None: ...
```

Registry pattern: `DIALECT_REGISTRY: dict[str, type[QueryEngine]]`. `get_engine(workspace)` returns the right concrete adapter — that is the only place dialect-aware code branches.

**Introspection per dialect (v1):**

| Dialect  | Tables / Columns | Foreign keys |
|---|---|---|
| Postgres | `information_schema.tables` (exclude `pg_catalog`, `information_schema`) + `information_schema.columns` | `pg_catalog.pg_constraint` join `pg_attribute` |
| SQLite | `SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'`, then `PRAGMA table_info(<t>)` per table | `PRAGMA foreign_key_list(<t>)` |

**Sampling rules** (in `services/schema_profiler.py`, shared across dialects):
- **ID heuristic** — name matches `^(id|.*_id|uuid|guid)$` OR is PK OR is unique monotonic int → `is_id=True`, skip value profiling.
- **Categorical** — string-ish OR `(distinct_count / row_count) < 0.05` AND `distinct_count <= 50` → `SELECT col FROM t GROUP BY col LIMIT 51` (the 51st row signals "more than 50, truncated").
- **Numeric (non-ID, non-categorical)** — `SELECT MIN, MAX, AVG, STDDEV` in one round-trip per table (batched across columns).
- **Other** — `COUNT(*) FILTER (WHERE col IS NOT NULL)` + a `LIMIT 5` sample.
- Persist as jsonb keyed by `(workspace_id)`; one row per workspace, full bundle inside.

---

## Read-only Defense in Depth

**Layer 1 — Boundary.** "Connect database" form shows the exact read-only GRANT recipe per dialect inline (Postgres: `CREATE ROLE querymind_ro LOGIN PASSWORD '…'; GRANT CONNECT … TO querymind_ro; GRANT USAGE ON SCHEMA … TO querymind_ro; GRANT SELECT ON ALL TABLES IN SCHEMA … TO querymind_ro; ALTER DEFAULT PRIVILEGES IN SCHEMA … GRANT SELECT ON TABLES TO querymind_ro;`; SQLite: open with `mode=ro` URI). After connect-test, run `CREATE TEMP TABLE _qm_probe(x int)` and **expect it to fail** — success → red banner "your creds have write access; we strongly recommend a read-only role."

**Layer 2 — Parse (`services/readonly_validator.py`).** `sqlglot.parse_one(sql, read=dialect)` must yield a `Select` (or a `With` whose final `this` is `Select` with no DML inside CTEs). Walk the AST and reject any of: `Insert / Update / Delete / Drop / Alter / Create / Truncate / Grant / Revoke / Call / Use / Set / Merge / Copy / Command`. Reject system-table refs by lowercased qualified-name match against a denylist: `pg_authid`, `pg_shadow`, `pg_settings`, `information_schema.user_privileges`, `pg_catalog.pg_user`. Reject dangerous functions: `pg_sleep`, `pg_read_file`, `lo_import`, `lo_export`, `dblink`, `load_file`. Multi-statement input → reject (sqlglot's `parse` returns a list; we require length 1). Inject `LIMIT 1000` on outermost `SELECT` if missing.

**Layer 3 — Runtime wrapper** (in each `engines/<dialect>.py::execute`):

| Dialect | Read-only incantation | Timeout |
|---|---|---|
| Postgres | `BEGIN; SET LOCAL transaction_read_only = on; SET LOCAL statement_timeout = '10s'; SET LOCAL idle_in_transaction_session_timeout = '15s'; <sql>; ROLLBACK;` | `statement_timeout=10s` |
| SQLite | Connection opened with `file:<path>?mode=ro` URI; `PRAGMA query_only = ON;` defense-in-depth | `sqlite3` progress handler aborts after N ops + asyncio `wait_for(timeout=10)` |

Client-side: results truncated to 1000 rows or 1 MB JSON (whichever first), `truncated=True` flag added to the response.

**What each layer catches:** L1 = LLM bypasses a validator bug → DB role refuses write. L2 = saves a network round-trip on malicious queries and stops `SET` side effects. L3 = runtime guard against semantically expensive selects, `pg_sleep`, parser blind spots.

---

## LangGraph Node Graph

```python
# backend/app/agents/state.py
class GraphState(TypedDict, total=False):
    user_id: UUID
    session_id: UUID
    user_message: str
    active_workspace_id: UUID | None         # from dropdown
    resolved_workspace_id: UUID | None       # set by coordinator
    table_scope: str | None                  # "chat with this table"
    intent: Literal["chitchat","metadata","data_query","dashboard","clarify"]
    schema_bundle: SchemaBundle | None
    pruned_schema: PrunedSchema | None       # top-K tables for the planner
    plan: SqlPlan | None
    validation: ValidationResult | None
    result: ResultSet | None
    chart: UISpec | None
    answer: AnswerDraft | None
    ui_spec: UISpec | None
    planner_attempts: int
    executor_attempts: int
    error: ErrorInfo | None
    latency_ms: dict[str, int]
```

Edges:

```
START → coordinator
coordinator -[chitchat]→ answer_writer → finalizer → END
coordinator -[clarify]→ finalizer (text_only UISpec asking which workspace) → END
coordinator -[metadata]→ schema_loader → answer_writer → finalizer → END
coordinator -[data_query|dashboard]→ schema_loader → query_planner
query_planner → query_validator
query_validator -[ok]→ query_executor
query_validator -[fail, attempts<2]→ query_planner (with rejection reason in state.error)
query_validator -[exhausted]→ error_responder → finalizer → END
query_executor -[ok]→ {chart_designer, answer_writer}     ← parallel fan-out
query_executor -[fail, attempts<2]→ query_planner (with DB error text)
query_executor -[exhausted]→ error_responder → finalizer → END
{chart_designer, answer_writer} → finalizer → END
```

Parallel fan-out uses LangGraph's reducer semantics — `chart` and `answer` are independent state slots merged by `finalizer`.

---

## Workspace Resolution (dropdown + inline)

```python
# backend/app/services/workspace_resolver.py
def resolve(msg: str, dropdown_id: UUID | None, user_workspaces: list[Workspace]) -> Resolution:
    by_name = {w.name.lower(): w.id for w in user_workspaces}
    explicit = re.findall(r"(?:@|\[)([A-Za-z0-9_\- ]+)\]?", msg)
    explicit_ids = [by_name[e.lower()] for e in explicit if e.lower() in by_name]
    if explicit_ids:
        return Resolved(explicit_ids[0]) if len(set(explicit_ids)) == 1 else Ambiguous(explicit_ids)
    bare = [wid for name, wid in by_name.items() if re.search(rf"\b{re.escape(name)}\b", msg, re.I)]
    if len(set(bare)) == 1:
        return Conflict(dropdown_id, bare[0]) if dropdown_id and bare[0] != dropdown_id else Resolved(bare[0])
    if dropdown_id:
        return Resolved(dropdown_id)
    return Missing()
```

Coordinator turns non-`Resolved` outcomes into `intent="clarify"` and emits a `text_only` UISpec with quick-reply chips listing candidate workspaces.

---

## vLLM + Structured Output

**Serving command (GPU 0; GPU 1 reserved as hot standby):**

```
vllm serve google/gemma-3-4b-it \
  --tensor-parallel-size 1 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.85 \
  --guided-decoding-backend xgrammar \
  --enable-prefix-caching \
  --port 8000
```

**LLM call shape** — OpenAI-compatible:

```python
client.chat.completions.create(
    model="google/gemma-3-4b-it",
    messages=[...],
    response_format={
        "type": "json_schema",
        "json_schema": {"name": "SqlPlan", "schema": SqlPlan.model_json_schema(), "strict": True},
    },
    temperature=0.1,
)
```

**Pydantic contracts (`backend/app/schemas/llm_io.py`):**

```python
class IntentDecision(BaseModel):
    intent: Literal["chitchat","metadata","data_query","dashboard","clarify"]
    workspace_hint: str | None = None

class SqlPlan(BaseModel):
    dialect: Literal["postgres","sqlite"]
    sql: str
    rationale: str
    expected_columns: list[str]

class AnswerDraft(BaseModel):
    headline: str
    body_md: str
    key_numbers: list[KeyNumber] = []
```

**Prompt assembly:**
- **Planner**: condensed schema = top-8 tables by BM25 over `f"{table} {col}"` keyed on question terms (any table named in the message is pinned). Drop sampled values first if over 6K tokens.
- **Chart designer**: receives result *shape* only (columns, dtypes, row_count, 5 sample rows) — never the full result. Emits `UISpec` directly.
- **Answer writer**: same shape data + question + headline metric. Emits `AnswerDraft`.

---

## App Data Model (Postgres metadata store)

```sql
users(id uuid PK DEFAULT gen_random_uuid(), email text UNIQUE NOT NULL,
      password_hash text NOT NULL, created_at timestamptz DEFAULT now())

workspaces(id uuid PK, user_id uuid FK users ON DELETE CASCADE,
           name text NOT NULL, dialect text NOT NULL CHECK (dialect IN ('postgres','sqlite')),
           host text, port int, db_name text, ssl bool DEFAULT false,
           status text DEFAULT 'pending',                        -- pending|profiling|ready|error
           created_at, updated_at,
           UNIQUE(user_id, name))

workspace_credentials(workspace_id uuid PK/FK workspaces ON DELETE CASCADE,
                      username text, ciphertext bytea, nonce bytea, key_version smallint DEFAULT 1)

schema_bundles(id uuid PK, workspace_id uuid FK UNIQUE,
               bundle jsonb NOT NULL, profiled_at timestamptz, version int DEFAULT 1)

chat_sessions(id uuid PK, user_id uuid FK, default_workspace_id uuid FK NULL,
              title text, created_at, updated_at)

messages(id uuid PK, session_id uuid FK ON DELETE CASCADE,
         role text CHECK (role IN ('user','assistant','system')),
         content text, ui_spec jsonb NULL,
         workspace_id uuid FK NULL, table_scope text NULL,
         latency_ms int NULL, created_at)

query_history(id uuid PK, message_id uuid FK ON DELETE CASCADE,
              workspace_id uuid FK, dialect text, query text,
              took_ms int, row_count int, status text,            -- ok|rejected|error|timeout
              error text NULL, validator_findings jsonb NULL, created_at)

profile_jobs(id uuid PK, workspace_id uuid FK,
             status text,                                          -- pending|running|done|error
             started_at, finished_at, error text NULL)

settings(user_id uuid PK/FK, vllm_endpoint text DEFAULT 'http://localhost:8000/v1',
         model text DEFAULT 'google/gemma-3-4b-it', updated_at)
```

Indexes: `messages(session_id, created_at)`, `query_history(workspace_id, created_at)`, `workspaces(user_id, name)`. Single Alembic migration `0001_initial.py`. Use `pgcrypto` for `gen_random_uuid()`.

**Crypto** — `services/crypto.py` wraps `cryptography.hazmat.primitives.ciphers.aead.AESGCM` with a 256-bit master key from env `QM_MASTER_KEY` (base64). `key_version` is reserved for future rotation; v1 hard-codes 1.

---

## UISpec Contract

```python
# backend/app/schemas/ui_spec.py
class TextOnly(BaseModel):  type: Literal["text_only"]; body_md: str
class KPI(BaseModel):       type: Literal["kpi"]; label: str; value: float|str
                            unit: str|None = None; delta: float|None = None; sparkline: list[float] = []
class BarSpec(BaseModel):   type: Literal["bar"]; title: str; x: str; y: list[str]
                            data: list[dict[str, Any]]; stacked: bool = False
class LineSpec(BaseModel):  type: Literal["line"]; title: str; x: str; y: list[str]; data: list[dict]
class PieSpec(BaseModel):   type: Literal["pie"]; title: str; label: str; value: str; data: list[dict]
class TableSpec(BaseModel): type: Literal["table"]; columns: list[ColumnDef]; rows: list[list[Any]]
class Dashboard(BaseModel): type: Literal["dashboard"]; title: str
                            children: list[GridChild]            # {span:1..12, spec: UISpec}
UISpec = Annotated[TextOnly|KPI|BarSpec|LineSpec|PieSpec|TableSpec|Dashboard,
                   Field(discriminator="type")]
```

Frontend `components/RenderSpec.tsx` switches on `spec.type` → Recharts (`<BarChart>`, `<LineChart>`, `<PieChart>`), custom `<KPICard>` (Cyan→Violet sparkline), `<DataTable>`. Dashboards render a CSS `grid-cols-12` and recurse. Every visualization is wrapped in `<GlassPanel>` so all Neural-Dark styling lives in one place.

Below every assistant message: collapsible `<CodeBlock language="sql">` showing generated SQL (JetBrains Mono per design tokens) with a copy button.

---

## Project Layout

```
backend/
  app/
    main.py                          FastAPI factory, CORS, lifespan (warm pools, ping vLLM)
    config.py                        pydantic-settings (env)
    api/
      auth.py                        POST /auth/login,/register
      workspaces.py                  CRUD /workspaces, POST /workspaces/{id}/refresh
      chat.py                        POST /chat (SSE), GET /sessions/{id}
      schema.py                      GET /workspaces/{id}/schema (Schema Explorer feed)
      settings.py                    GET/PUT /settings
      deps.py                        JWT dep, db session dep
    engines/
      base.py                        QueryEngine ABC + ResultSet/SchemaBundle types
      postgres.py  sqlite.py         v1 implementations
      registry.py                    DIALECT_REGISTRY
    agents/
      graph.py                       LangGraph wiring
      state.py                       GraphState
      llm.py                         vLLM OpenAI client w/ guided JSON
      prompts/                       one file per node
      nodes/                         coordinator.py … finalizer.py
    services/
      crypto.py                      AES-GCM
      schema_profiler.py             orchestrates introspect + sample
      readonly_validator.py          sqlglot + AST walk
      schema_pruner.py               BM25 top-K
      workspace_resolver.py          inline/dropdown resolution
    db/
      models.py                      SQLAlchemy 2.0 ORM
      session.py                     async engine + session factory
      migrations/                    Alembic
    workers/
      celery_app.py
      profile_task.py
    schemas/
      llm_io.py  ui_spec.py  schema_bundle.py
  tests/
    unit/
      test_readonly_validator.py     malicious corpus + benign corpus
      test_schema_profiler.py
      test_workspace_resolver.py
      test_schema_pruner.py
    integration/
      test_e2e_postgres.py           docker-compose fixture, end-to-end chat
      test_e2e_sqlite.py
  pyproject.toml  requirements.txt  alembic.ini  .env.example

frontend/
  app/
    layout.tsx                       GlassBackground, font loading
    page.tsx                         Workspaces grid (workspaces.png)
    login/page.tsx  register/page.tsx
    workspaces/new/page.tsx          Connect-database form (connect_database.png)
    workspaces/[id]/schema/page.tsx  Schema Explorer (schema_explorer.png)
    chat/page.tsx                    Neural Chat (neural_chat.png)
    settings/page.tsx                vLLM endpoint + model
  components/
    GlassPanel.tsx  KPICard.tsx  CodeBlock.tsx  MessageBubble.tsx
    RenderSpec.tsx                   discriminated-union dispatcher
    WorkspacePicker.tsx              dropdown + @-mention autocomplete
    charts/{BarChart,LineChart,PieChart,DataTable}.tsx
    SchemaTree.tsx  TableCard.tsx
  lib/
    api.ts                           fetch + SSE helpers
    types.ts                         generated from OpenAPI
    tokens.ts                        mirrors DESIGN.md
  tailwind.config.ts                 extends colors from DESIGN.md tokens

infra/
  docker-compose.dev.yml             postgres, redis, optional vllm sidecar
  docker-compose.test.yml            postgres + sqlite for integration tests
  seed/                              sample schema + data per dialect
```

**Critical files (the architecture lives or dies on these):**
- `backend/app/engines/base.py` — the `QueryEngine` ABC; the entire dialect abstraction depends on getting this right the first time.
- `backend/app/services/readonly_validator.py` — security-critical; test corpus is the spec.
- `backend/app/agents/graph.py` — LangGraph wiring with retry loops and chart/answer fan-out.
- `backend/app/schemas/ui_spec.py` — frontend/backend contract; any churn ripples through both sides.
- `frontend/components/RenderSpec.tsx` — discriminated-union dispatcher rendering LLM output.

---

## Build Order

1. **Foundations** — `pyproject.toml`, FastAPI skeleton, Postgres + Alembic, JWT auth, frontend scaffold with Tailwind tokens, dev docker-compose.
2. **QueryEngine + read-only validator** — `engines/base.py` + `postgres.py` + `sqlite.py` + `services/readonly_validator.py` with the malicious-corpus unit tests (TDD: write the tests first, they encode the security spec).
3. **Schema profiler + Celery worker** — `services/schema_profiler.py`, `workers/profile_task.py`, `POST /workspaces` end-to-end (creates row, enqueues job, persists bundle).
4. **vLLM bring-up** — install vLLM, launch Gemma-3-4B-IT on GPU 0, verify `/v1/chat/completions` with a `response_format` JSON-schema call from a curl smoke test.
5. **LangGraph agents** — `graph.py`, `state.py`, `nodes/*.py`, planner/validator retry loop with mocked LLM, then wire to real vLLM.
6. **Chat API + SSE streaming** — `POST /chat` invokes the graph and streams node-level events.
7. **Frontend pages** — Workspaces grid, Connect form, Schema Explorer, Neural Chat (with `RenderSpec`, `CodeBlock`, `WorkspacePicker`), Settings.
8. **Verification (see below).**

---

## Verification

**Unit — `test_readonly_validator.py`** must REJECT every line in this corpus (encoded in the test file):

```
DROP TABLE users;
SELECT 1; DROP TABLE users;
WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x;
SELECT * FROM pg_authid;
SELECT pg_sleep(60);
SELECT load_file('/etc/passwd');
SELECT * INTO OUTFILE '/tmp/x' FROM users;
COPY users TO '/tmp/leak.csv';
CREATE TEMP TABLE t AS SELECT * FROM users;
GRANT ALL ON users TO public;
TRUNCATE users;
UPDATE users SET pw='x';
MERGE INTO t USING s ON ...;
SELECT * FROM dblink('...','DROP TABLE x');
```

…and must ACCEPT a benign corpus (joins, CTEs, window functions, `GROUP BY ROLLUP`, `LATERAL`, parameterized analytics queries).

**Integration — `test_e2e_postgres.py` / `test_e2e_sqlite.py`** via pytest fixture that boots `docker-compose.test.yml` (or uses an in-process SQLite for the SQLite case):
1. Seed a `sales(order_id, customer_id, ts, amount, region)` dataset.
2. `POST /workspaces` → assert `profile_jobs` row reaches `done`, bundle contains the expected tables/columns/samples (DISTINCT for `region`, MIN/MAX for `amount`/`ts`, 5-sample for everything else).
3. `POST /chat` with `"total revenue by region last 30 days"` → assert final SSE event contains a `bar` or `table` `ui_spec`, `query_history.status="ok"`, and the captured SQL re-passes the validator.

**Manual smoke (via the `run` skill once code exists):** boot the dev stack, register a user, create `Core_Analytics` (Postgres demo data), open Neural Chat, ask "Show me the revenue trend for the last quarter." Expected: line chart, 2-3 sentence narrative, collapsible SQL block, all rendered against the Neural-Dark glassmorphism per `ui_images/neural_chat.png`.

---

## Shipped (post-v1)

This section tracks what landed AFTER the original v1 build order
above. PLAN.md stays the source of truth; new work edits this list.

* **Phase 1 — Multi-connection workspaces.** A `Workspace` is now a
  folder holding N `WorkspaceConnection`s, each its own dialect +
  encrypted creds + schema bundle + RAG chunks. Alembic migration 0003
  + 0004. UI ships a per-workspace connection list with Test / Add /
  Re-profile / Delete.
* **Phase 2 — Elasticsearch engine.** `engines/elasticsearch.py`,
  `services/es_readonly_validator.py`, JSON envelope `{index, body}`
  emitted by the planner. Validator hard-rejects `script`, system
  indices, mutation endpoints. ES client pinned `>=8,<9` for protocol
  compatibility.
* **Phase 3 — Federation layer.** `coordinator → multi_schema_loader →
  federated_planner → federated_executor` path. `FederatedPlan`
  decomposes a question into N parallel sub-queries plus a merge
  pipeline (join / union / concat). Pure-Python merge under
  `services/federation_merge.py` — no pandas dependency. UI shows the
  per-sub-query breakdown via `FederationBadge`.
* **Phase 3.1 — Polish.** Per-connection BM25 prune in
  `federated_planner`; `FEDERATION_MAX_ROWS` cap on the merged
  ResultSet (default 1000); SSE `final` event carries `sub_results`
  metadata.
* **Phase 4 — Dialect adapters.** MySQL (`asyncmy`),
  ClickHouse (`clickhouse-connect`), MongoDB (`motor` + JSON-pipeline
  validator under `mongo_readonly_validator.py`), Oracle (`oracledb`
  thin mode). Seven dialects in the registry; UI form supports all.
* **Phase 5 — docker-compose dialects + seeds.** Commented-out service
  blocks for MySQL / ClickHouse / Mongo / Oracle with matching seed
  scripts mirroring the Postgres `seed_sales` shape. Per-dialect
  mocked-driver smoke tests under `tests/unit/test_<dialect>_engine.py`.
* **Phase 6 — Triton bge-m3.** `infra/triton/` ships a Python-backend
  model serving `BAAI/bge-m3` (1024-d multilingual) on CPU torch.
  `services/rag/triton_client.py` is the only speaker of the v2
  inference API; embeddings flow through `indexer.py` into pgvector.
* **Phase 7 — Production hardening.** `_check_secrets()` fail-fasts
  when `QM_ENVIRONMENT=production` is set with dev-default JWT or
  master key. SSE error sanitization (`_sanitize_error_for_client`)
  strips SQL / connection strings / driver internals out of the
  user-facing message. `slowapi` rate limits: `/chat` 10/min,
  `/auth/*` 5/min. Prometheus `/metrics` via
  `prometheus-fastapi-instrumentator` + custom `qm_*` counters in
  `metrics.py`. End-to-end `federated_executor` smoke test (mocked
  engines, parallel fan-out, merge-pipeline correctness).
* **Phase 8 — Production deploy artifacts.** `infra/k8s/` ships 12
  bare manifests (namespace, secret template, configmap, postgres
  StatefulSet, redis, backend, celery worker + beat, frontend,
  migrations Job, nginx Ingress with SSE-safe annotations, Prometheus
  ServiceMonitor). `infra/helm/querymind/` is the parameterized chart
  (`helm lint` clean). `DEPLOY.md` is the prod walkthrough.
* **Phase 9 — Observability + UX polish.** `qm_llm_calls_total{node,
  outcome}` wired at every `LLMClient.structured` call (ok / repair /
  failed). Frontend toast system replaces inline error text for
  non-recoverable failures.

Tests: 196 unit tests passing as of the last shipped phase.

## Explicitly Deferred (still open)

- MSSQL / Snowflake / BigQuery / DuckDB dialects — slot open in
  `engines/`, no driver picked.
- Multi-replica vLLM with autoscaling — single replica handles current
  load; revisit when sustained QPS justifies the GPU split.
- Query-result caching — no Redis cache layer for SQL outputs yet.
- RBAC beyond per-user isolation — no shared workspaces, no roles, no
  org/team grouping.
- BYO model fine-tuning / LoRA hot-swap.
- Scheduled dashboards / email digests.
- Token-level chat streaming — node-level SSE events are still enough.
- Cross-workspace joins (federation works WITHIN a single workspace's
  connections, not across workspaces).

---

## Known Risks & Mitigations

- **Gemma-3-4B may produce subtly wrong SQL** (hallucinated columns, wrong dialect). Mitigation: schema is literally in the prompt; validator catches structural issues; planner ↔ validator retry loop catches semantic issues. Expect ~5-15% retry rate — budget for it in the demo script.
- **vLLM cold start ~30s.** Keep the server always running during the demo; FastAPI health-checks it on startup and refuses chat requests until ready.
- **L40S idle power.** Both GPUs powered on but only GPU 0 actively serving — acceptable for hackathon hardware.
- **Greenfield risk.** No prior code reuse means ~3 extra engineering days compared to salvaging. Mitigation: build order above front-loads the security-critical pieces (validator, engine adapter) so the riskiest code is also the most tested.
