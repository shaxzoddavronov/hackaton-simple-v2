# TASKS — QueryMind AI hackathon backlog

Autonomous loop tracker. Update on every commit that closes a phase;
keep a **Resume from** pointer when leaving mid-flight so the next
session picks up cleanly. Phases run sequentially in this file; the
assistant moves to the next queued item without asking.

Legend: ✅ done · 🔧 in progress · ⏳ queued · ⏸ blocked

---

## Shipped (recent)

- ✅ Phase 30 — BigQuery dialect adapter (`62d4261…0ca043a`)
- ✅ Phase 31 — MCP server (stdio + HTTP)
- ✅ Phase 32 — GraphQL connector (13th dialect)
- ✅ Phase 33 — Webhook notification destinations for scheduled reports
- ✅ Phase 34 — Query result export (CSV / Excel / JSON)
- ✅ Schema audit + alembic upgrade to head (0010 → 0022)
- ✅ Bugfix: chart_designer picks bar over table for category + multi-numeric (`b8dbf46`)
- ✅ Bugfix: engines populate `ResultSet.dtypes` (postgres asyncpg-prepared, sqlite/mongo/es value-inferred) so chart heuristics actually fire (`0ca043a`)

## Now — currently in flight

- 🔧 **Phase 42 — Scope picker (frontend half)** + **Phase 43 —
  Neural Dark refresh** queued for the next turn.
  Phase 42 backend just shipped; the chat UI still needs the
  scope dropdown that calls `/workspaces/{id}/clusters` and
  passes `scope` / `scope_cluster_id` in the chat payload.
  Phase 43 WIP frontend files (login / header / panels) are in
  the working tree, uncommitted.

## Queued (user-added 2026-06-04)

- ⏳ **Phase 41 — Row-budget guard validator**
  Reject queries that would scan or return billion / trillion-row
  results before they reach the cluster. Sits next to the existing
  read-only / DSL validators. Estimates row scan from the planner's
  emitted SQL using the schema's `row_count_estimate`; rejects when
  predicted scan > configurable ceiling (default 10M). For
  Elasticsearch / Mongo, parse the aggregation shape and refuse
  unbounded `match_all` / no-`$limit` pipelines on indices /
  collections above the ceiling. Surfaces to the user as a
  text_only with a "narrow your filter" hint.

- ⏳ **Phase 42 — Scope picker in workspace chat**
  Let the user choose the scope of a question:
  - table (one table inside one connection — most narrow)
  - database (all tables in one connection)
  - all databases (every connection in this workspace)
  - cluster (a connection group representing one DB cluster —
    new concept; needs a ClusterMembership table)
  - all clusters (every cluster in the workspace)
  - all connections (synonym for "all databases" if no clusters)

  UI: a scope dropdown next to the current connection picker;
  selecting "all databases" or wider triggers the federation path
  (`multi_schema_loader → federated_planner → federated_executor`)
  with the wider connection set. Backend additions:
  - new ConnectionCluster table + endpoint family
  - extend GraphState.resolved_connection_id → resolved_scope
    (one_of {table, db, all_dbs, cluster, all_clusters})
  - federation merge handles N-way concat / union for wide scopes

## Shipped this session (continued)

- ✅ **Phase 35 — Connection health monitoring + status badge**
  - migration 0023 adds 4 health columns + partial index on
    unhealthy rows
  - `services/connection_health.py::probe_one` dispatches per
    dialect (SQL SELECT 1, ES cluster.health, Mongo ping, GraphQL
    `__typename`, REST head/get)
  - `workers/health_task.py::run_health_sweep` Celery beat every
    5 minutes; sanitised errors, skip-when-no-creds outcome path
  - `GET /workspaces/{w}/connections/{c}/health?refresh=` endpoint
  - frontend `<ConnectionStatusDot>` with green/red/grey + ↻ button
  - 12 new unit tests; suite 738 passed

- ✅ **Phase 36 — Conversation memory pruning + summary**
  - migration 0024 adds `chat_sessions.summary` jsonb
  - `services/conversation_summary.py::ensure_summary` rolls the
    older portion of a long session into one LLM-summarised
    paragraph; threshold=30, keep_recent=10
  - falls back to truncated transcript when vLLM is down so the
    chat path never breaks
  - api/chat.py prepends the summary as a `role=system` item to
    `conversation_history` before invoking the graph
  - 13 new unit tests covering threshold gates, transcript shape,
    LLM happy/sad path, injected-client override; suite 751 passed

- ✅ **Phase 42 — Scope picker (backend half)**
  - Migration 0027 adds `connection_clusters` table + nullable
    `workspace_connections.cluster_id` (FK ON DELETE SET NULL)
  - `ConnectionCluster` model + relationship; FK on
    `WorkspaceConnection.cluster_id`
  - `api/clusters.py` CRUD + membership endpoints under
    `/workspaces/{id}/clusters` (create / list / read / patch /
    delete + POST and DELETE `/members`)
  - `ChatRequest.scope` (`table | database | all_databases |
    cluster | all_clusters | all_connections`) + optional
    `scope_table` / `scope_cluster_id`
  - `services/scope_resolver.py::resolve_scope` translates the
    enum into a concrete `connection_ids[]` plus a `federation`
    flag; empty result with a clear error when the scope can't
    resolve (no active conn for `database`, no clusters for
    `all_clusters`, etc.)
  - UUID round-trip helper `_as_uuid` makes the resolver work
    under both Postgres (returns UUID) and SQLite (returns str)
  - 11 new unit tests covering every scope branch + the empty /
    error paths; suite 878 passed (was 867)
  - **Wiring to the chat event-stream defers to a follow-up
    commit** so the federation-input expansion lands together
    with the frontend dropdown.

- ✅ **Phase 41 — Row-budget guard validator**
  - `services/row_budget_validator.py::validate_row_budget` —
    per-dialect predicted-scan check
  - SQL path: sqlglot AST walk → tables touched → sum
    `row_count_estimate` from the bundle → reject when over
    `Settings.MAX_PREDICTED_ROWS` (default 10M) AND no LIMIT /
    lone-aggregate / TOP escape hatch
  - ES path: bare `match_all` against an over-cap index without
    `size` or `aggs` → reject
  - Mongo path: pipeline with no `$limit` / `$group` / `$count`
    against an over-cap collection → reject
  - REST API + GraphQL: skipped (external systems, HTTP timeout
    is the only ceiling)
  - Wired into `agents/nodes/query_validator.py` after the
    read-only / DSL check so a security finding always wins
  - Advisory when no bundle / no estimates — never gate-blocks
    a fresh / unprofiled connection
  - 25 new unit tests covering every pass + reject path;
    suite 867 passed (was 842)

- ✅ **Phase 16 — username + access/refresh token auth (closed out)**
  - Foundation commit at `82a76fc` (login/refresh/logout/me,
    admin user CRUD, audit middleware, refresh-token rotation,
    bootstrap super-user)
  - `test_auth_tokens.py` (14 tests) — fixed offset-naive
    datetime bug in `consume_refresh_token` (SQLite strips tz)
  - `test_audit.py` (14 tests) — middleware integration via
    FastAPI TestClient, including `_BrokenSession` proof that
    audit failures never break the user response
  - `test_admin.py` (16 tests) — superuser 403 gate, CRUD round
    trips, self-protect (no self-deactivate / self-demote /
    self-delete), audit listing filters. Hardened the admin
    handlers to str-coerce both sides of the self-protect
    compare so the guard holds across Postgres ↔ SQLite UUID
    representations.
  - Public `/register` page replaced with an "ask your
    administrator" panel; `registerUser` API helper now throws.
  - New admin UI pages: `/admin/users` (list, create, toggle
    active, toggle superuser, reset password, delete) and
    `/admin/audit` (filterable timeline by action prefix +
    status). Self-row buttons disabled to match the backend's
    400 guards.
  - AppHeader hides the Admin link unless `/auth/me` reports
    `is_superuser=true`.
  - i18n bundles extended with `nav_admin` across uz/ru/en;
    parity test passes.
  - Backend suite 842 passed (was 798); frontend type-check
    clean.

- ✅ **Phase 40 — Multi-language UI (i18n) — uz / ru / en**
  - `lib/i18n/messages.ts` — flat `Messages` type + 3 bundles
    (uz/ru/en); function-valued keys for plurals and node-name
    interpolation
  - `lib/i18n/context.tsx` — `I18nProvider`, `useT()`, `useLocale()`;
    SSR-safe (server pass = en), client effect picks up
    `localStorage` / `navigator.language`
  - `components/LocaleSwitcher.tsx` — UZ / RU / EN pill in the
    top bar
  - `app/layout.tsx` wraps the tree with `I18nProvider`;
    `AppHeader` reads nav labels + sign-in/out copy from the
    bundle and renders the switcher
  - parity test (`tsx` runner) locks bundle-key drift +
    no-empty-string invariant; ran clean: "i18n bundles: OK"
  - Backend regression suite 798 passed (no change)

- ✅ **Phase 39 — Slash commands in chat**
  - `services/slash_commands.py` parser + 6 handlers:
    `/help`, `/sql` (echo last SQL), `/lang uz|ru|en`,
    `/clear-cache`, `/refresh-schema`, `/explain`
  - api/chat.py short-circuits before the agent graph when a
    slash command is detected — no vLLM round-trip
  - side-effects (cache invalidation + ProfileJob enqueue) run
    behind defensive try/except so a Redis hiccup never breaks
    the chat path
  - assistant turn is persisted so commands appear in history
    like normal answers
  - 17 new unit tests covering parser edge cases + every
    handler's happy / sad / no-arg path; suite 798 passed

- ✅ **Phase 38 — Question similarity recall via qa_history chunks**
  - migration 0026 widens `rag_chunks.kind` CHECK to include
    `'qa_history'`
  - `services/qa_history.py` — `index_qa_pair` (embed + INSERT
    after successful turn) + `find_similar` (cosine-distance search
    above threshold=0.85, top-K=3)
  - Triton failure short-circuits both — chat path stays alive
  - api/chat.py emits new `similar` SSE event BEFORE agent run;
    indexes `(question, headline)` after the assistant message
    lands (only for data_query / dashboard / metadata / federated
    intents)
  - `_extract_headline` helper picks a short label from any
    UISpec variant (kpi label+value, text_only first line, chart
    title, dashboard recurse)
  - frontend chat page renders a chip rail with the top hits;
    clicking populates the input
  - 14 new unit tests for threshold gate, min-length skip, Triton
    failure swallow, INSERT/rollback path, JSON-string metadata
    decode; suite 781 passed

- ✅ **Phase 37 — Per-workspace usage metrics dashboard**
  - migration 0025 adds `usage_daily` table (workspace_id, day, +
    7 BigInteger counters, PK = (workspace_id, day))
  - `services/usage.py` ContextVar-bound `UsageBucket`; recording
    sites in agents/llm.py (token in/out), query_executor (ok/fail
    + cache_hit), rag_retriever (retrievals); chat.py opens +
    flushes the bucket per request
  - flush UPSERTs via Postgres ON CONFLICT, swallows DB errors so
    a usage hiccup never breaks the chat
  - `GET /workspaces/{id}/usage?days=30` returns per-day rows +
    totals; clamped to [1, 365]
  - frontend `/workspaces/[id]/usage` page with 4 stat cards,
    daily-LLM bar sparkline, full breakdown table
  - 16 new unit tests covering ContextVar isolation across tasks,
    no-op fallback without bucket, token-clamping, empty-bucket
    skip, DB-error swallow; suite 767 passed

## Queued

- ⏳ **Phase 37 — Per-workspace usage metrics dashboard**
  Tally tokens consumed, queries executed, RAG retrievals, cache hits
  per workspace per day. New `usage_daily` table; admin endpoint
  `GET /workspaces/{id}/usage?from=&to=`. Frontend chart.

- ⏳ **Phase 38 — Question similarity recall**
  Embed every successfully-answered question + its headline into
  `rag_chunks` with `kind='qa_history'`. Before planning, semantic
  search against this index; if score > 0.85, surface a chip
  "Similar to: ‹old question›" with a one-click re-run.

- ⏳ **Phase 39 — Slash commands in chat**
  `/sql` show raw SQL, `/refresh schema`, `/clear cache`,
  `/explain` show LLM's reasoning trace, `/lang uz|ru|en` switch
  answer language. Parse in `coordinator` before intent routing.

(no other queued phases right now — backlog complete from the
original plan; next session should pick up either the frontend
redesign work that the redesign brief enables, or open follow-ups
the user adds.)

## Blocked

- ⏸ **Phase 16 — Username + access/refresh token auth** (user's own work)
  Don't touch: `api/auth.py`, `api/deps.py`, `api/admin.py`,
  `services/audit.py`, `services/auth_tokens.py`, migration 0010,
  `main.py`, the Phase 16 portions of `models.py`.

- ⏸ **Triton infra files** (held back per user)
  Don't commit: `infra/triton/Dockerfile`,
  `infra/triton/README.md`, `infra/triton/config_cpu.pbtxt`,
  `infra/triton-local/`, `infra/docker-compose.local.yml`.

---

## Resume conventions

When pausing a phase mid-flight, edit the **Resume from** line of
that phase to point at:
- the last committed step, AND
- the next concrete file/function to touch, AND
- any open question that's blocking forward progress.

A future session should be able to resume from this file alone
without re-reading the conversation history.
