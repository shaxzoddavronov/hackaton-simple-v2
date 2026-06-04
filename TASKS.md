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

(empty — Phase 40 just shipped; loop reads the queue next)

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
