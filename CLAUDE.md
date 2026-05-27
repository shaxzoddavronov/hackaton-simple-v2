# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

Waves 1–5 from `PLAN.md` plus several extensions are landed:

- **Wave 1–5**: backend FastAPI app, SQLAlchemy/Alembic metadata DB, agent (9 LangGraph nodes), Celery profiling worker, Next.js 14 frontend (auth, workspaces, chat SSE, schema explorer, settings).
- **Wave 6 — RAG layer**: pgvector store, Triton-served bge-m3 embeddings, daily schema-diff job, `rag_retriever` node between `schema_loader` and `query_planner`.
- **Phase 1 — multi-connection workspaces**: a Workspace is now a folder holding N WorkspaceConnections, each its own dialect + creds + schema bundle + RAG chunks. Migration 0003 + 0004.
- **Phase 2 — Elasticsearch engine**: non-SQL adapter with JSON-DSL validator. Planner emits envelope `{"index","body"}`; validator blocks `script`, `_delete_by_query`, system indices, etc.
- **Phase 3 — Federation layer**: `coordinator → multi_schema_loader → federated_planner → federated_executor` path. `FederatedPlan` decomposes cross-DB questions into N parallel sub-queries plus a merge pipeline (join / union / concat) implemented in pure-Python `services/federation_merge.py`. UI shows per-sub-query breakdown via `FederationBadge`.
- **Phase 4 — Dialect adapters**: MySQL (asyncmy), ClickHouse (clickhouse-connect), MongoDB (motor + JSON-pipeline validator), Oracle (oracledb thin mode). All seven dialects in the registry; UI form supports all.

`PLAN.md` remains the source of truth for what hasn't shipped yet — read it before adding scope.

## Product in one paragraph

**QueryMind AI** — users connect their own database (Postgres or SQLite in v1), the backend deterministically introspects the schema, and a LangGraph agent generates **read-only** SQL from natural-language questions, executes it, and returns a written answer plus an optional chart spec. It is self-hosted and runs **only** against a local vLLM server (`google/gemma-3-4b-it` with `xgrammar` guided decoding) — no external AI APIs.

## Architecture invariants (do not violate without updating PLAN.md)

- **No external LLM APIs.** vLLM at `http://localhost:8000/v1` is the only model endpoint. Structured output goes through `response_format={"type":"json_schema", ...}` against Pydantic-derived schemas — see `app/agents/llm.py::LLMClient.structured`. Never parse free-text JSON from the model.
- **Dialect abstraction lives in `backend/app/engines/`.** Every concrete adapter satisfies the `QueryEngine` Protocol in `base.py` and registers itself via `@register("…")`. Seven dialects ship today: `postgres`, `sqlite`, `mysql`, `clickhouse`, `oracle` (SQL family — validated by `sqlglot`); `elasticsearch`, `mongodb` (non-SQL — validated by their own JSON-DSL validators in `services/`). Code outside `engines/` (planner + validator + executor) branches on dialect ONLY at the three legal dispatch points: choosing the planner system prompt, choosing the validator, and choosing the engine. New SQL dialects are a copy-paste of `postgres.py` + driver swap. New NoSQL dialects need their own validator + planner prompt; mirror `mongodb.py` for the shape.
- **Read-only is defense in depth, all three layers required:**
  1. Boundary — the connect form documents the read-only GRANT recipe and probes write access.
  2. Parse — `services/readonly_validator.py` uses `sqlglot.parse(sql, read=dialect)`, walks the AST, rejects any DML/DDL/`SET`/`COPY`/`GRANT`/multi-statement input and a denylist of system tables and dangerous functions (`pg_sleep`, `pg_read_file`, `dblink`, `load_file`, …). The malicious corpus in `tests/unit/test_readonly_validator.py` is the spec — it must reject every line.
  3. Runtime — Postgres executes inside `conn.transaction(readonly=True)` with `SET LOCAL statement_timeout` + `idle_in_transaction_session_timeout`. SQLite opens with `file:<path>?mode=ro` URI plus `PRAGMA query_only=ON`.
- **Agent graph topology** (`backend/app/agents/graph.py`): `coordinator → schema_loader → query_planner → query_validator → query_executor → {chart_designer, answer_writer} → finalizer`. Planner↔validator and planner↔executor each have **retry≤2** (`MAX_PLANNER_ATTEMPTS`, `MAX_EXECUTOR_ATTEMPTS`), then route to `error_responder`. `chart_designer` and `answer_writer` fan out in parallel via LangGraph reducer semantics — `chart` and `answer` are independent state slots (`_take_last` reducer in `state.py`) merged in `finalizer`.
- **LLM-driven nodes:** `coordinator`, `query_planner`, `chart_designer`, `answer_writer`. **Deterministic nodes:** `schema_loader`, `query_validator`, `query_executor`, `finalizer`, `error_responder`. Don't move a node between groups without a reason.
- **Frontend/backend contract is `backend/app/schemas/ui_spec.py`.** `UISpec` is a discriminated union (`text_only | kpi | bar | line | pie | table | dashboard`). `frontend/components/RenderSpec.tsx` dispatches on `spec.type` and uses TS exhaustiveness checking. Any change to one side must update the other (plus `frontend/lib/types.ts`) in the same PR.
- **Chart designer never sees raw result rows.** It receives only the result *shape* (columns, dtypes, row_count, 5 sample rows). Same for `answer_writer`. This is for prompt size and to prevent the LLM from inventing numbers.
- **LLM I/O contracts live in `backend/app/schemas/llm_io.py`** (`IntentDecision`, `SqlPlan`, `AnswerDraft`). Every vLLM call must go through `LLMClient.structured(messages, response_model=Model)` — never parse free-text JSON from the model.
- **Planner prompt size is gated by `services/schema_pruner.py`** (BM25 top-K over `f"{table} {col}"`, K=8). Any table named in the user message is pinned. Drop sampled values before dropping table entries when over budget (~6K tokens).
- **Workspace resolution** (`services/workspace_resolver.py`) merges three signals — dropdown selection, `@name`/`[name]` mentions, and bare-word matches against the user's workspace names. Anything that isn't a clean `Resolved` becomes `intent="clarify"` and a `text_only` UISpec with quick-reply chips. Don't bypass this — the coordinator and `api/chat.py::_resolve_or_workspace_id` depend on its outcomes.
- **DB credentials are encrypted at rest with AES-GCM** via `services/crypto.py`, master key from env `QM_MASTER_KEY` (base64, 256-bit). `key_version` column is reserved for rotation; v1 hard-codes 1.
- **Chat streaming is node-level SSE events only.** `POST /chat` returns `event: session | node | final | error` frames (see `api/chat.py::event_stream`). Token-level streaming is explicitly out of v1 — don't add it. Frontend renders the final `UISpec` once the `final` event arrives.
- **All settings come from `app/config.py::Settings`.** Never read `os.environ` directly elsewhere.
- **The metadata DB uses dialect-portable column types** (`models.py`: `JSONType = JSONB().with_variant(JSON(), "sqlite")`, `UUIDType` similarly). Unit tests run on in-memory SQLite, prod runs on Postgres — keep both green.
- **Embeddings are Triton-only.** `services/rag/triton_client.py` is the sole speaker of the Triton v2 inference API; nothing else should call Triton. vLLM never embeds; Triton never does LLM. The default model is `bge-m3` (1024d, multilingual) — set `TRITON_EMBED_MODEL` + `EMBEDDING_DIM` to swap.
- **RAG store is pgvector inside the existing metadata DB** (`rag_chunks` table, `vector(1024)` column on Postgres, JSON list on SQLite for tests). HNSW cosine index on the embedding column. Four chunk `kind`s: `schema_table`, `schema_column`, `api_endpoint`, `user_doc`. Workspace-scoped chunks set `workspace_id`; global chunks (our own REST routes) use NULL.
- **RAG retraining triggers, in priority order:**
  1. Workspace creation → `profile_task` enqueues `run_index_workspace` after the bundle lands (always full reindex).
  2. Document upload → `POST /documents` enqueues `run_index_document`.
  3. Daily Celery Beat at `RAG_DIFF_CHECK_HOUR_UTC` (default 00:00 UTC) → `run_daily_diff` introspects every `status='ready'` workspace, compares to the stored bundle via `services/rag/differ.py`, and re-profiles + enqueues a re-index on any structural change. `api_endpoint` chunks are reindexed unconditionally at the same beat.
  4. The retriever short-circuits on `content_hash` match, so re-running the indexer on an unchanged bundle costs zero Triton calls.
- **Agent reads RAG before planning.** `rag_retriever` runs for `intent in {data_query, dashboard}` only. It embeds the user message once, queries top-K (`RAG_TOP_K=12`), then writes `pruned_table_qnames` (semantic) and `retrieved_chunks` (full payloads for the planner prompt). On Triton failure it returns `{}` so the planner falls back to BM25 — **never crash the graph when Triton is down**.
- **Diff granularity is structural only.** `services/rag/differ.py` ignores sample values and row-count estimates by design — otherwise every daily run on a busy prod DB would re-embed. Added/removed columns, renamed columns (via column-set hashing), and added/removed FKs all count as drift.

## Critical files (the architecture lives or dies on these)

- `backend/app/engines/base.py` — `QueryEngine` Protocol + `SchemaBundle`/`ResultSet`/`ValidationResult` types.
- `backend/app/services/readonly_validator.py` — security-critical; test corpus is the spec, write tests first.
- `backend/app/agents/graph.py` — retry loops + parallel fan-out routing.
- `backend/app/agents/state.py` — `GraphState` TypedDict with reducer-annotated parallel slots.
- `backend/app/schemas/ui_spec.py` — cross-stack contract.
- `backend/app/schemas/llm_io.py` — Pydantic models that become the vLLM JSON-schema; renaming a field changes the LLM contract.
- `backend/app/agents/llm.py` — only place that talks to vLLM; enforces guided decoding.
- `frontend/components/RenderSpec.tsx` — discriminated-union dispatcher with exhaustiveness check.
- `frontend/lib/api.ts` — sole HTTP/SSE client; no other module should `fetch()` the API directly.
- `backend/app/services/rag/triton_client.py` — sole speaker of the Triton inference API; embedding-only.
- `backend/app/services/rag/indexer.py` — only writer to the `rag_chunks` table; uses `content_hash` to skip unchanged rows.
- `backend/app/services/rag/retriever.py` — sole reader of `rag_chunks` for semantic search; falls back to BM25 on Triton failure.
- `backend/app/services/rag/differ.py` — structural-only schema fingerprint; gate for daily re-index.
- `backend/app/agents/nodes/rag_retriever.py` — pre-planner node; runs only on data_query/dashboard intents.
- `backend/app/workers/diff_task.py` — Celery Beat daily entrypoint; refreshes bundles + enqueues re-index on drift.

## Build order (front-loads risk)

PLAN.md §"Build Order" is canonical. The ordering is deliberate: security-critical pieces (read-only validator, engine adapter) come **before** LLM wiring so the riskiest code is also the most-tested. Don't reorder to start with LLM work first.

## Design system

`ui_images/DESIGN.md` defines the "Neural Dark" tokens (colors, typography, spacing). All visualizations are wrapped in `<GlassPanel>` so glassmorphism styling lives in one place. Fonts: Space Grotesk (headlines), Inter (body), JetBrains Mono (data + SQL code blocks). Below every assistant message in chat is a collapsible `<CodeBlock language="sql">` showing the generated SQL.

## Commands

### Backend

Run from `backend/` (the working directory most commands assume):

```bash
# Install (Python 3.11–3.12)
pip install -e ".[dev]"          # or: pip install -r requirements.txt && pip install pytest pytest-asyncio pytest-cov

# Serve the API on :8080 (frontend CORS expects this port)
uvicorn app.main:app --reload --port 8080

# Run all tests
pytest

# Run one test file / one test
pytest tests/unit/test_readonly_validator.py
pytest tests/unit/test_readonly_validator.py::test_rejects_drop

# Postgres e2e (auto-skips if test Postgres not on localhost:55432)
pytest tests/integration/test_e2e_postgres.py -s

# Migrations (DATABASE_URL must be set; alembic.ini's sqlalchemy.url is blank by design)
alembic upgrade head
alembic revision --autogenerate -m "add foo"

# Celery worker (schema-profiling jobs)
celery -A app.workers.celery_app.celery_app worker --loglevel=info
```

### Frontend

Run from `frontend/`:

```bash
npm install
npm run dev          # http://localhost:3000
npm run build
npm run lint
npm run type-check   # tsc --noEmit
```

The frontend talks to `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8080`).

### Infra

```bash
# Dev stack: Postgres :5432 + Redis :6379 (vLLM stays on host for GPU)
docker compose -f infra/docker-compose.dev.yml up -d

# Ephemeral test Postgres on :55432 (consumed by the integration test)
docker compose -f infra/docker-compose.test.yml up -d

# vLLM on the host (needs your GPU)
vllm serve google/gemma-3-4b-it --guided-decoding-backend xgrammar --max-model-len 8192 --port 8000

# Curl-based e2e smoke against a running backend (health → register → login → workspace → chat SSE)
./infra/smoke_test.sh
```

### Required env

Copy `.env.example` → `.env` (at repo root, the backend's `Settings` reads from there). At minimum set `QM_MASTER_KEY` (generate with `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`) and `JWT_SECRET`. `DATABASE_URL` defaults to a local SQLite file, which is fine for unit tests but use the Postgres URL for any real work.
