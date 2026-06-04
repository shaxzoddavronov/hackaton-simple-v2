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

(empty — Phase 37 just shipped; loop will pick up Phase 38 next)

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

- ⏳ **Phase 40 — Multi-language UI (i18n)**
  Uzbek (Latin) / Russian / English. Next-i18next + JSON message
  bundles. Locale switcher in the top bar, persisted to user
  settings. Server-side: answer-writer node already adapts to the
  user's language; just plumb the locale through.

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
