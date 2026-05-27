# QueryMind AI

Self-hosted natural-language SQL/NoSQL over your own databases. Ask a
question in plain English (or Uzbek, or Russian — the agent matches
your language); QueryMind translates it into **read-only** SQL or DSL,
executes it against the right connection, and returns a written
summary plus a chart or table.

Runs entirely on local infrastructure — no external AI APIs.

## What it does

* **Multi-dialect** — seven engines plugged in today:
  Postgres, SQLite, MySQL, ClickHouse, Oracle (SQL family);
  Elasticsearch, MongoDB (non-SQL, JSON DSL).
* **Multi-connection workspaces** — a workspace is a folder holding
  N connections. The agent picks the right one per question, or fans
  out a **federated query** across two or more (cross-DB inner join,
  union, or concat — pure-Python merge, no extra service).
* **Read-only defense in depth, per dialect** — at the parse layer
  (`sqlglot` AST for SQL, JSON validators for ES / Mongo), at the
  runtime layer (`SET TRANSACTION READ ONLY`, ES `readonly` setting,
  Mongo pipeline allow-list), and at the boundary (the connect form
  documents the read-only role recipe and probes write access).
* **RAG over your schema** — daily `schema-diff` Celery beat detects
  drift and re-embeds. `BAAI/bge-m3` served by an in-stack Triton; agent
  pulls top-K relevant tables before planning. Falls back to BM25 if
  Triton is offline — the chat never crashes on RAG outage.
* **Generative UI** — the answer arrives as a discriminated-union
  `UISpec` (text, KPI card, bar, line, pie, table, dashboard) rendered
  by Next.js + Recharts. Chart variant is chosen deterministically
  from the result shape.
* **Production-ready** — rate-limited /chat (Redis-backed slowapi),
  Prometheus `/metrics`, JWT auth with bcrypt, AES-GCM at-rest
  encryption for connection credentials, Helm chart for k8s deploys.

## Architecture (one screen)

```
                       ┌──────────────────┐
                       │   Next.js 14     │
                       │   chat + workspace│
                       └─────────┬────────┘
                                 │ SSE
                  POST /chat ┌───▼──────────────────────────────┐
                             │   FastAPI (uvicorn) :8080        │
                             │                                  │
                             │  Auth  Rate-limit  /metrics      │
                             │              │                   │
                             │              ▼                   │
                             │      LangGraph agent             │
                             │                                  │
                             │  coordinator                     │
                             │      │                           │
                             │      ├─ schema_loader → rag      │
                             │      │       ↓        retriever  │
                             │      │  query_planner ◄────┐     │
                             │      │       ↓             │     │
                             │      │  query_validator ───┘     │
                             │      │       ↓                   │
                             │      │  query_executor           │
                             │      │       ↓                   │
                             │      │  chart + answer           │
                             │      │       ↓                   │
                             │      │  finalizer (UISpec)       │
                             │      │                           │
                             │      └─ multi_schema_loader →    │
                             │              federated_planner → │
                             │              federated_executor →│
                             │              (parallel)          │
                             └───┬───────────┬───────────┬──────┘
                                 │           │           │
                          ┌──────▼─┐   ┌─────▼─┐  ┌──────▼──┐
                          │Postgres│   │ Redis │  │ Celery  │
                          │+pgvect │   │       │  │ worker+ │
                          └────┬───┘   └───────┘  │ beat    │
                               │ rag_chunks       └─────────┘
                               │                       │
                               │            ┌──────────┘
                               │            │ embedding
                          ┌────▼────────────▼─┐
                          │ Triton (bge-m3)   │
                          └───────────────────┘

       ┌─ Postgres ─┐  ┌─ MySQL ─┐  ┌─ Clickhouse ─┐  ┌─ Oracle ─┐
       │   user DB  │  │ user DB │  │   user DB    │  │ user DB │
       └────────────┘  └─────────┘  └──────────────┘  └─────────┘
       ┌─ Elasticsearch ─┐  ┌─ MongoDB ─┐
       │     user DB     │  │  user DB  │
       └─────────────────┘  └───────────┘
```

LLM goes through any OpenAI-compatible endpoint (default: local vLLM
serving Qwen 3 Coder; the user-tested `Qwen/Qwen3-Coder-30B-A3B-Instruct`
on a separate GPU host is the reference setup).

## Quickstart (dev)

Requirements: Docker, Python 3.11+, Node 18+, a reachable OpenAI-
compatible LLM endpoint.

```bash
# 1. Bring up Postgres + Redis (+ Triton if you want RAG).
docker compose -f infra/docker-compose.dev.yml up -d

# 2. Backend
cd backend
cp .env.example .env   # fill in JWT_SECRET, QM_MASTER_KEY, VLLM_*
python -m pip install -r requirements.txt
alembic upgrade head
python -m uvicorn app.main:app --reload --port 8080 &
celery -A app.workers.celery_app:celery_app worker --pool=solo &
celery -A app.workers.celery_app:celery_app beat &

# 3. Frontend
cd ../frontend
npm install
npm run dev -- -p 3001    # backend already owns :3000-equivalents

# 4. Browser
open http://localhost:3001
```

Register → workspaces → New workspace → Add connection → Test → Add.

## Production deploy

See [`DEPLOY.md`](DEPLOY.md) for the k8s + Helm walkthrough. Two paths:

* **Bare manifests** under `infra/k8s/` — `kubectl apply -f` in order.
* **Helm chart** under `infra/helm/querymind/` — `helm install` with
  the two required secrets:
  ```bash
  helm install querymind infra/helm/querymind \
    --namespace querymind --create-namespace \
    --set secrets.jwtSecret=$(openssl rand -base64 48) \
    --set secrets.qmMasterKey=$(openssl rand -base64 32)
  ```

## Adding a new dialect

The contract is `backend/app/engines/base.py::QueryEngine`. The cheapest
path:

1. Add the driver to `requirements.txt`.
2. Copy `backend/app/engines/postgres.py` to `<dialect>.py`.
3. Replace the driver-specific bits in `_connect`, `introspect_schema`,
   and `execute`. Map driver dtype codes in `_<dialect>_dtype` helper.
4. `@register("<dialect>")` decorator + add to
   `backend/app/engines/__init__.py::register_all`.
5. Add the dialect to the CHECK constraints in `app/db/models.py`
   (and a new Alembic migration).
6. For non-SQL dialects: write a JSON validator under
   `app/services/<dialect>_readonly_validator.py` mirroring
   `mongo_readonly_validator.py`, then dispatch in
   `query_validator.py`, `federated_executor.py`, and add a
   `_<DIALECT>_SYSTEM` planner prompt in `query_planner.py`.
7. Frontend: add the dialect to `frontend/lib/api.ts::Dialect` and
   `frontend/app/workspaces/[id]/page.tsx::DIALECTS`. Write the form
   fields.
8. Mock-driver smoke test under `tests/unit/test_<dialect>_engine.py`.

Reference engines: mysql / clickhouse / oracle (SQL); elasticsearch /
mongodb (non-SQL).

## Repo layout

```
backend/
  app/
    agents/           LangGraph nodes + state + LLM client
    api/              FastAPI routers (auth, chat, workspaces, …)
    db/               SQLAlchemy models + Alembic migrations
    engines/          QueryEngine adapters (one file per dialect)
    schemas/          Pydantic LLM-IO + UISpec (frontend contract)
    services/         crypto, RAG, schema profiler, validators, …
    workers/          Celery tasks (profile, index, daily diff)
  tests/unit/         196 tests; no DB / no LLM required
frontend/             Next.js 14 App Router
infra/
  docker-compose.dev.yml   Postgres + Redis + Triton + commented dialects
  k8s/                     Bare K8s manifests
  helm/querymind/          Helm chart
  triton/                  Triton image + bge-m3 model_repository
  seed/                    Per-dialect demo datasets
PLAN.md               Source of truth for unshipped scope
DEPLOY.md             Production deployment walkthrough
CLAUDE.md             Architecture invariants (for AI assistants + new devs)
```

## Tests

```bash
cd backend
python -m pytest tests/unit -q \
  --ignore=tests/unit/test_graph_smoke.py \
  --ignore=tests/unit/test_crypto.py
```

The two ignored files need extra fixtures (`openai` SDK live, a real
`QM_MASTER_KEY` in env). The rest — 196 tests — run hermetic.

## License

MIT. See `LICENSE`.
