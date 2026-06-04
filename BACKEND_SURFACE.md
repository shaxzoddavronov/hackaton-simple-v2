# BACKEND_SURFACE.md — QueryMind AI backend capability map

**For:** frontend redesigners (Claude design + humans).
**Purpose:** a complete inventory of what the backend can do, what
data it returns, what state it tracks, what events it streams —
so the redesigned frontend can cover every capability without
hunting through the codebase.

QueryMind AI is a self-hosted **natural-language-to-SQL agent**
that connects to a user's databases / docs / APIs and answers
questions in chat. The backend is FastAPI + LangGraph; the agent
graph dispatches across 13 dialects of query engine. RAG is
pgvector-backed; embeddings come from a local Triton (bge-m3);
LLM is a local vLLM (Qwen3-Coder-30B by default).

The current frontend (Next.js 14 + Tailwind, "Neural Dark" theme)
works but was hand-built phase-by-phase. This document is the
single source of truth a designer needs to start from scratch.

---

## 0 — Mental model

Three concepts, in order of nesting:

1. **User** owns N **Workspaces**.
2. A **Workspace** is a folder that holds N **WorkspaceConnections**
   (each connection = one database / API). A workspace can also hold
   **DocSources** (folders, URLs, IMAP mailboxes, etc. that get
   crawled into the RAG index) and **Dashboards** (grids of saved
   questions). Chat sessions live at workspace level — when the
   user picks a connection mid-chat, the agent dispatches the next
   turn to that connection's dialect.
3. A **ChatSession** belongs to a workspace and remembers its last
   connection. Each turn produces one assistant **Message** which
   carries a structured **UISpec** the frontend renders.

The agent itself is a 9-node LangGraph DAG. The frontend never has
to know about the DAG — it sends a chat message, gets back an SSE
stream of node events ending in a `final` event carrying the
UISpec.

---

## 1 — Authentication

Phase 16. JWT access + refresh tokens. The frontend stores the
access token in memory + the refresh token in `httpOnly` cookie.

### Endpoints

| Method | Path                          | Body / Query              | Returns                                  |
|--------|-------------------------------|---------------------------|------------------------------------------|
| POST   | `/auth/register`              | `{username, email, password}` | `{access_token, refresh_token, user}`  |
| POST   | `/auth/login`                 | `{username_or_email, password}` | `{access_token, refresh_token, user}` |
| POST   | `/auth/refresh`               | `{refresh_token}`         | `{access_token, refresh_token, user}`    |
| POST   | `/auth/logout`                | —                         | 204                                      |
| GET    | `/auth/me`                    | —                         | `{id, username, email, role, created_at}` |

`role` ∈ `{"user", "admin"}`. Most endpoints are scoped to the
calling user; the admin endpoints (`/admin/*`) require
`role="admin"`.

### Admin surface

| Method | Path                          | Returns                                  |
|--------|-------------------------------|------------------------------------------|
| POST   | `/admin/users`                | Create a user                            |
| GET    | `/admin/users`                | List users                               |
| GET    | `/admin/users/{id}`           | One user                                 |
| PATCH  | `/admin/users/{id}`           | Update (role / disable)                  |
| DELETE | `/admin/users/{id}`           | Hard delete                              |
| GET    | `/admin/audit`                | Audit log entries                        |

Audit log captures every privileged action (user create/delete,
role change, etc.) with `actor_id`, `action`, `target_id`,
`metadata`, `created_at`.

### Frontend UX surfaces this implies

- Login + register screens.
- Header avatar with username + "sign out".
- An admin-only "Users" page (visible to `role="admin"`).
- An admin "Audit log" page with a filterable timeline.

---

## 2 — Workspaces

A workspace is a named folder. The most-recent connection used in
chat is remembered per session so reloading a chat thread restores
the right DB selector.

### Endpoints

| Method | Path                          | Returns                                  |
|--------|-------------------------------|------------------------------------------|
| POST   | `/workspaces`                 | Create workspace `{name}`               |
| GET    | `/workspaces`                 | List the user's workspaces               |
| GET    | `/workspaces/{id}`            | One workspace                            |
| DELETE | `/workspaces/{id}`            | Delete + cascade                         |
| GET    | `/workspaces/{id}/usage?days=30` | Usage rollup (Phase 37)              |

### Statuses

`workspaces.status` ∈ `{"pending", "profiling", "ready", "error", "auth_error"}`.
At workspace level this is an aggregate hint; the canonical
per-DB status is on each connection.

### Frontend UX surfaces

- Workspaces list (left nav or grid).
- "+ New workspace" entry.
- One detail page per workspace with these sub-sections:
  - Connections (see §3)
  - Document sources (see §4)
  - Dashboards (see §6)
  - Usage (see §11)
  - Settings / danger zone (delete workspace)

---

## 3 — Connections (the heart)

A connection is one **database or API endpoint** inside a workspace.
Thirteen dialects ship today, each with its own metadata and auth
shape. Credentials are **AES-GCM encrypted at rest** with a master
key from env (`QM_MASTER_KEY`) — the API never returns plaintext
credentials, only the auth_kind hint.

### Endpoints

| Method | Path                                                | Purpose                       |
|--------|------------------------------------------------------|-------------------------------|
| POST   | `/workspaces/{id}/connections/test`                  | Probe before saving           |
| POST   | `/workspaces/{id}/connections`                       | Create (enqueues profile job) |
| GET    | `/workspaces/{id}/connections`                       | List + health snapshot        |
| GET    | `/workspaces/{id}/connections/{cid}`                 | One connection                |
| DELETE | `/workspaces/{id}/connections/{cid}`                 | Delete + cascade              |
| POST   | `/workspaces/{id}/connections/{cid}/refresh`         | Re-introspect schema          |
| GET    | `/workspaces/{id}/connections/{cid}/health?refresh=` | Health probe (cached or live) |
| GET    | `/workspaces/{id}/connections/{cid}/schema`          | Browse the introspected schema|

### Dialects (13)

The dialect determines the UI form for connection details and the
SQL/JSON shape the planner emits.

| Dialect         | Family    | UI form needs                                                                 |
|-----------------|-----------|-------------------------------------------------------------------------------|
| `postgres`      | SQL       | host, port, db, user, password (or DSN)                                       |
| `sqlite`        | SQL       | file path                                                                     |
| `mysql`         | SQL       | host, port, db, user, password                                                |
| `clickhouse`    | SQL       | host, port, db, user, password, TLS toggle                                    |
| `oracle`        | SQL       | host, port, service_name, user, password                                      |
| `duckdb`        | SQL       | file path OR in-memory                                                        |
| `mssql`         | SQL       | host, port, db, user, password, encrypt toggle                                |
| `snowflake`     | SQL       | account, user, password, warehouse, database, schema, role                    |
| `bigquery`      | SQL       | project, dataset, location, service-account JSON (multiline textarea)         |
| `mongodb`       | NoSQL     | host, port, db_name, user, password (optional), replica_set, TLS              |
| `elasticsearch` | NoSQL     | endpoint URL, auth (basic / API key / none)                                   |
| `rest_api`      | API       | base_url, spec source (preset / OpenAPI URL / OpenAPI file), auth (4 kinds)   |
| `graphql`       | API       | endpoint URL, auth (bearer / api_key / basic / none)                          |

REST API has presets the form should expose: `generic`, `bitrix24`,
`amocrm`, `hubspot`, `1c_odata`. Selecting a preset hints the
agent at the right pagination / filter conventions.

### Auth kinds

Per connection: `{"password", "dsn", "iam", "none", "bearer", "api_key", "basic", "oauth2_client"}`.
The form should switch sub-fields based on this.

### Statuses on each connection

`{"pending", "profiling", "ready", "error", "auth_error"}`.

### Health (Phase 35)

Each connection row carries the result of the periodic 5-minute
liveness probe:

```ts
{
  last_health_check_at: string | null;     // ISO timestamp
  last_health_ok: boolean | null;          // null = never probed
  last_health_latency_ms: number | null;
  last_health_error: string | null;        // short, UI-safe
}
```

The frontend's recheck button hits the `?refresh=true` endpoint for
an on-demand probe.

### Frontend UX surfaces

- "+ Add connection" wizard: dialect select → dialect-specific form
  → "Test connection" → "Save". On save the connection lands as
  `pending`, profiles in the background, eventually flips to
  `ready` (or `error`/`auth_error`).
- Connection card with:
  - Health dot (green / red / grey) + latency tooltip + recheck ↻
  - Status badge (`ready` / `profiling` / `error` / …)
  - Actions: Re-profile, View schema, Delete, Edit (TBD — currently delete + recreate)
- Schema explorer per connection: tree of tables → columns with
  inferred dtypes + sampled distinct values + FKs visualised.

---

## 4 — Document sources (RAG ingestion)

A DocSource is something that gets crawled into the RAG index. The
agent later cites these chunks alongside SQL results.

### Nine source kinds

| Kind         | Input shape                                                                   |
|--------------|-------------------------------------------------------------------------------|
| `folder`     | local path (server-side). Watcher does inotify on Linux, ReadDirectoryChangesW on Windows. |
| `url_list`   | newline-separated URLs. Crawled with httpx + BeautifulSoup.                   |
| `db_column`  | connection_id + table + file_column. Row context tagged on each chunk so citations link back to the source row. |
| `smb`        | UNC path + username/password                                                  |
| `gdrive`     | Google service-account JSON + folder ID                                       |
| `onedrive`   | OAuth device-code flow (button: "Authorise OneDrive")                         |
| `imap`       | server, port, ssl, user, password, mailbox/folder                             |
| `slack`      | upload a Slack workspace export zip                                           |
| `telegram`   | upload a Telegram chat export zip                                             |

### Supported file types

PDF (text + OCR fallback via tesseract), DOCX, XLSX, CSV/Parquet,
HTML, plain text, MP3/MP4/M4A/WAV/WEBM/OGG (Whisper transcription
via faster-whisper), PNG/JPG (OCR).

### Endpoints

| Method | Path                                                  | Purpose                  |
|--------|--------------------------------------------------------|--------------------------|
| POST   | `/workspaces/{id}/doc-sources`                         | Create                   |
| GET    | `/workspaces/{id}/doc-sources`                         | List                     |
| DELETE | `/workspaces/{id}/doc-sources/{sid}`                   | Delete                   |
| POST   | `/workspaces/{id}/doc-sources/{sid}/crawl`             | Manual crawl now         |
| POST   | `/documents`                                           | Upload a single document |
| GET    | `/documents`                                           | List uploads             |
| DELETE | `/documents/{id}`                                      | Delete a document        |
| POST   | `/cloud-auth/onedrive/start`                           | Begin OneDrive device flow |
| POST   | `/cloud-auth/onedrive/poll`                            | Poll the flow            |
| POST   | `/data-files`                                          | Upload CSV/Parquet → DuckDB connection |

### DocSource row shape

```ts
{
  id: string;
  workspace_id: string;
  source_kind: SourceKind;
  config: Record<string, unknown>;    // shape varies by kind
  status: "idle" | "harvesting" | "ready" | "error";
  last_harvested_at: string | null;
  last_error: string | null;
  doc_count: number;                   // how many files indexed so far
}
```

### Frontend UX surfaces

- Tabbed picker (9 tabs or a select) for which kind to add.
- Per-kind config form (different fields per tab).
- For OneDrive: a "Sign in" button that opens the device-code
  flow in a popup or shows the user code + verification URL inline.
- List view of all sources with status badge, last-crawled time,
  doc count, "Crawl now" button, "Delete".
- A separate uploads tray for one-off documents.

---

## 5 — Chat (the main interaction)

The chat endpoint is **SSE**, not WebSocket. The frontend sends a
POST with the message; the response is a stream of
`event: <name>\ndata: <json>\n\n` frames terminated by `final` or
`error`.

### Request

```http
POST /chat
Authorization: Bearer <token>
Content-Type: application/json

{
  "message": "oxirgi 30 kunda eng faol foydalanuvchi",
  "session_id": "<uuid or null for new session>",
  "active_workspace_id": "<uuid>",
  "active_connection_id": "<uuid or null>"
}
```

`Accept: text/event-stream`.

### SSE event sequence

The frame order is:

1. **`session`** — always first; carries `{session_id, workspace_id, connection_id}`.
   Sent even for brand-new sessions so the client can pin the URL.
2. **`similar`** *(Phase 38, optional)* — only if past Q-A pairs match.
   Carries `{hits: [{message_id, session_id, question, headline, similarity}]}`.
3. **`node`** *(0..N)* — one per agent-graph node firing.
   Carries `{node: string}`. Useful for a "currently doing X" indicator.
4. **`final`** — one. Carries the answer:
   ```ts
   {
     ui_spec: UISpec | null;
     sql: string | null;             // the SQL the executor ran (or JSON envelope for non-SQL dialects)
     assistant_message_id: string;
     sub_results: Record<string, {columns: string[]; row_count: number}>;  // federation breakdown
     citations: Citation[];
   }
   ```
5. **`error`** — alternative to `final` on agent failure.
   Carries `{message: string}` (sanitised — never raw SQL / creds).

For **slash commands** (Phase 39 — see §10) the stream is shorter:
just `session` → `final` with no graph nodes in between.

### Chat session endpoints

| Method | Path                                       | Purpose                         |
|--------|--------------------------------------------|---------------------------------|
| GET    | `/chat/sessions?workspace_id=&limit=`      | List the user's sessions        |
| GET    | `/chat/sessions/{sid}`                     | One session + all messages      |
| DELETE | `/chat/sessions/{sid}`                     | Delete + cascade                |
| GET    | `/chat/messages/{mid}/export?format=csv|xlsx|json` | Download result rows  |

### Message row shape (as returned by `/chat/sessions/{id}`)

```ts
{
  id: string;
  role: "user" | "assistant" | "system";
  content: string;                  // plain text (assistant: the headline / fallback body)
  ui_spec: UISpec | null;           // structured rendering payload
  created_at: string;
}
```

---

## 6 — UISpec — the cross-stack contract

Every assistant turn returns one `UISpec`. The discriminator is
`type`. Variants:

### `text_only`

```ts
{ type: "text_only"; body_md: string }
```

Plain markdown. Used for chitchat, clarify, slash-command
responses, error explanations.

### `kpi`

```ts
{
  type: "kpi";
  label: string;
  value: number | string;
  unit?: string | null;
  delta?: number | null;
  sparkline?: number[];
}
```

Big-number callout. Used when the executor returned exactly one
row × one numeric column.

### `bar`

```ts
{
  type: "bar";
  title: string;
  x: string;                        // column name on x-axis
  y: string[];                      // 1+ numeric columns
  data: Array<Record<string, any>>; // row-oriented
  stacked: boolean;                 // default false
}
```

Picked when: many rows × 1 categorical + ≥1 numeric. Multiple
numerics → grouped (or stacked if user said so).

### `line`

```ts
{
  type: "line";
  title: string;
  x: string;                        // a time / date column
  y: string[];                      // 1+ numeric columns
  data: Array<Record<string, any>>;
}
```

Picked when: many rows × time + numerics (no categorical anchor).

### `pie`

```ts
{
  type: "pie";
  title: string;
  label: string;                    // category column
  value: string;                    // single numeric column
  data: Array<Record<string, any>>;
}
```

Picked when: 2-8 rows + 1 cat + 1 numeric + question mentions
share / percent / distribution / ulush / tarqalish.

### `table`

```ts
{
  type: "table";
  columns: ColumnDef[];
  rows: any[][];
}
ColumnDef = {
  key: string;
  label: string;
  dtype: "int" | "float" | "string" | "bool" | "datetime" | "date";
  align: "left" | "right" | "center";
}
```

Fallback for any other shape. Always supported.

### `dashboard`

```ts
{
  type: "dashboard";
  title: string;
  children: Array<{
    span: 1..12;                    // 12-column grid
    spec: UISpec;                   // recursive
  }>;
}
```

Composite — emitted for the `dashboard` intent. The frontend lays
out children on a 12-column grid.

---

## 7 — Citations (RAG transparency)

When the planner used non-schema RAG chunks (uploaded docs, API
endpoint catalogs, prior Q-A pairs), the `final` SSE event carries
a `citations[]` array. Each citation:

```ts
{
  kind: "user_doc" | "api_endpoint" | "qa_history" | "harvested_doc";
  source_key: string;               // human-readable: "policy.pdf", "GET /api/v1/users", ...
  preview: string;                  // ~200 chars of the matched chunk
  score: number;                    // 0..1
  // When the chunk traces back to a DB row (db_column source):
  db_row?: {
    connection_id: string;
    table: string;
    row_pk: Record<string, any>;
    file_column: string;
    file_reference: string;
  };
}
```

The frontend should render citations as a chip strip under each
assistant message with hover-preview and a click-to-open behaviour
for `db_row` (deep-link to the schema explorer's row view, when
available).

---

## 8 — Federation (cross-connection questions)

When the user asks something that needs more than one connection in
the same workspace ("compare our pg quiz volume with the ES search
volume"), the planner emits a `FederatedPlan` and the executor fans
out N parallel sub-queries, then merges them via `join`/`union`/
`concat`.

The `final` SSE event's `sub_results` field surfaces the
per-sub-query breakdown:

```ts
sub_results: {
  "pg_orders": { columns: ["order_id", "total"], row_count: 12 },
  "es_clicks":  { columns: ["user_id", "clicks"], row_count: 30 }
}
```

The frontend should render this as a `<FederationBadge>` above the
chart: "Queried: pg-quiz · 12 rows, es-search · 30 rows".

---

## 9 — Saved questions + Dashboards (Phase 26-27)

A user can ⭐ any assistant message to save the upstream question
as a `SavedQuestion`. Saved questions can be grouped into a
`Dashboard`. The dashboard page re-runs every saved question
through the agent on render so the data is always fresh (subject
to Phase 23 query cache).

### Endpoints

| Method | Path                                                 | Purpose                       |
|--------|------------------------------------------------------|-------------------------------|
| POST   | `/workspaces/{id}/saved-questions`                   | Star a question               |
| GET    | `/workspaces/{id}/saved-questions?dashboard_id=`     | List saved questions          |
| DELETE | `/workspaces/{id}/saved-questions/{qid}`             | Unstar                        |
| PATCH  | `/workspaces/{id}/saved-questions/{qid}`             | Reassign to a dashboard       |
| POST   | `/workspaces/{id}/dashboards`                        | Create a dashboard            |
| GET    | `/workspaces/{id}/dashboards`                        | List                          |
| GET    | `/workspaces/{id}/dashboards/{did}`                  | Dashboard + saved questions   |
| DELETE | `/workspaces/{id}/dashboards/{did}`                  | Delete                        |
| POST   | `/workspaces/{id}/dashboards/{did}/run`              | Re-run all questions          |

### Frontend UX surfaces

- A "⭐ Star question" button on each assistant turn (Phase 27).
- A modal for "save as" — title + which dashboard.
- A `/workspaces/[id]/dashboards` index.
- A `/workspaces/[id]/dashboards/[did]` detail page with each
  saved question rendered as its own UISpec card; "↻ Rerun" per
  card and "↻ Run all".
- A "Schedule report" button on the dashboard → opens a form for
  cron + email + webhook URLs (Phases 29 + 33).

---

## 10 — Slash commands (Phase 39)

Six commands the chat input recognises. They bypass the agent
entirely — `session` → `final` with a `text_only` UISpec.

| Command            | Action                                                              |
|--------------------|---------------------------------------------------------------------|
| `/help`            | Show the command list                                               |
| `/sql`             | Echo the SQL of the most recent assistant turn in this session      |
| `/lang uz|ru|en`   | Hint the preferred answer language for ambiguous turns              |
| `/clear-cache`     | Drop the Redis query cache for the current connection               |
| `/refresh-schema`  | Enqueue a re-profile for the current connection                     |
| `/explain`         | Short overview of how the agent answers                             |

Unknown `/foo` prompts fall through to the agent as regular
questions.

### Frontend UX surfaces

- Autocomplete dropdown when the user types `/` in the chat input.
- Each command's tooltip shows the description.

---

## 11 — Usage metrics dashboard (Phase 37)

Per-workspace per-day rollup of:

```ts
{
  day: string;                       // YYYY-MM-DD
  llm_calls: number;
  llm_tokens_in: number;
  llm_tokens_out: number;
  queries_ok: number;
  queries_failed: number;
  rag_retrievals: number;
  cache_hits: number;
}
```

GET `/workspaces/{id}/usage?days=N` returns `{days[], totals}`.

### Frontend UX surfaces

- A "Usage" tab in the workspace detail page.
- Cards for the 4 totals (LLM calls / tokens / queries / RAG +
  cache).
- A daily bar sparkline for LLM calls.
- A full per-day breakdown table.

---

## 12 — Scheduled email reports (Phase 29 + Phase 33)

Each dashboard can have N **ReportSchedules** that fire at a cron
expression. At fire-time the worker re-runs every saved question on
the dashboard, renders an HTML email digest, and dispatches via
SMTP. Phase 33 added webhook fan-out so each schedule can also POST
the rendered payload to Slack / MS Teams / Discord / Mattermost /
custom incoming-webhook endpoints — the payload shape is
auto-selected from the URL's host.

### ReportSchedule row

```ts
{
  id: string;
  owner_id: string;
  workspace_id: string;
  dashboard_id: string;
  cron: string;                      // 5-field standard cron
  recipients: string;                // CSV of extra email addresses
  webhook_urls: string;              // newline-separated URLs (Phase 33)
  enabled: boolean;
  last_fired_at: string | null;
  last_status: string | null;        // "ok" / "error"
  last_error: string | null;
}
```

### Frontend UX surfaces

- "+ Schedule report" button on the dashboard.
- Form: cron expression (with friendly preset buttons: daily 9am /
  weekly Mon 9am / monthly 1st), email recipients (textarea),
  webhook URLs (textarea, newline-separated, helper hints for
  Slack/Teams/Discord).
- A schedule list under the dashboard with enable/disable toggle,
  "Last fired at" + status, edit, delete.

---

## 13 — Saved-question similarity recall (Phase 38)

Before each new turn the chat path semantic-searches a
`qa_history` sub-index of prior Q-A pairs and emits a `similar`
SSE event with the top hits. The frontend should render these as
a chip rail above the input — clicking a chip populates the input
with the prior question for one-tap re-run.

```ts
SimilarHit = {
  message_id: string;
  session_id: string;
  question: string;
  headline: string;
  similarity: number;                // 0..1
}
```

Threshold ≥ 0.85 cosine. Max 3 chips. Multilingual (bge-m3) — the
prior question can be in another language than the current one.

---

## 14 — MCP server (Phase 31)

QueryMind exposes itself over the Model Context Protocol so
Claude Desktop and other MCP clients can call it. Three tools:

| Tool                          | Args                              | Returns                       |
|-------------------------------|-----------------------------------|-------------------------------|
| `querymind.list_workspaces`   | —                                 | `[{id, name, ...}]`           |
| `querymind.workspace_schema`  | `{workspace_id}`                  | Schema bundle                 |
| `querymind.ask`               | `{workspace_id, question}`        | The agent's final UISpec      |

Transport: stdio (Claude Desktop) + `POST /mcp` HTTP. Auth via
`Authorization: Bearer <jwt>` forwarded into the JSON-RPC
`params.token`.

### Frontend UX surfaces

- A "Connect Claude Desktop" page under Settings with a copy-paste
  block of the Claude Desktop config and a one-line `npx`-style
  command to install.

---

## 15 — Result export (Phase 34)

Every successful assistant message has a download dropdown that
fetches the cached result rows (stored on `query_history` for any
result ≤ 10k rows / 8 MiB).

GET `/chat/messages/{mid}/export?format=csv|xlsx|json`.

Status codes:
- 200 — payload follows
- 404 — message doesn't exist / not yours / no `query_history` row
- 410 — result was dropped (oversize). Re-ask to refresh
- 422 — bad `format`

### Frontend UX surfaces

- A "↓ Export" dropdown next to the SQL block on each assistant
  message. CSV is UTF-8 BOM (Excel-friendly). XLSX has styled
  headers + frozen first row. JSON is row-oriented.

---

## 16 — Settings (Phase 5)

| Method | Path             | Purpose                          |
|--------|------------------|----------------------------------|
| GET    | `/settings`      | Return the user's preferences    |

Per-user preferences: timezone, prefer_charts vs tables,
default_workspace_id, etc. The full list is a small JSON column on
the `users` row.

### Frontend UX surfaces

- A `/settings` page with sections: Profile, Preferences, MCP /
  Claude Desktop, API tokens, Danger zone (delete account).

---

## 17 — Background work surfaces

The frontend doesn't actually call Celery, but it should show
"profiling in progress" and "indexing N chunks" states. These
surface via:

1. `WorkspaceConnection.status` flipping `pending → profiling → ready`.
2. `DocSource.status` flipping `idle → harvesting → ready` plus
   `last_harvested_at` updating.
3. Health probes filling `last_health_*` columns every 5 min.

When a user takes an action that enqueues background work (create
connection, refresh schema, crawl now), the frontend should:
- Optimistically flip the local status to `profiling` / `harvesting`.
- Poll the connection / source GET endpoint every 2-3s until
  `status == "ready"` or `"error"`.

---

## 18 — Statuses, badges, colours

A consolidated palette of every status the UI surfaces:

| State            | Where               | Suggested colour |
|------------------|---------------------|------------------|
| `pending`        | workspace/connection| neutral          |
| `profiling`      | connection          | blue / activity  |
| `ready`          | connection/workspace| emerald          |
| `error`          | many                | rose             |
| `auth_error`     | connection          | amber            |
| `harvesting`     | doc source          | blue / activity  |
| `idle`           | doc source          | neutral          |
| health: ok       | connection dot      | emerald          |
| health: fail     | connection dot      | rose             |
| health: unknown  | connection dot      | neutral grey     |

---

## 19 — Errors & toasts

Every error the user sees is sanitised by the backend (`_sanitize_error_for_client`).
The frontend should treat error strings as opaque user-facing text
and never try to parse them.

Common shapes:

- 400 — validation error (bad form input)
- 401 — token expired / missing → trigger refresh
- 403 — not your resource → toast "Not found" (don't leak that it exists)
- 404 — not found
- 409 — conflict (duplicate name)
- 410 — gone (e.g., result rows expired)
- 413 — payload too large
- 422 — unsupported value (e.g., bad export format)
- 429 — rate-limited (chat: 10/min; auth: 5/min)
- 500 — backend bug → toast + Sentry/log link

---

## 20 — Pages this implies (suggested IA)

A maximal frontend would have:

1. `/login`, `/register`
2. `/` — landing (when logged in: redirect to last workspace or
   workspaces list)
3. `/workspaces` — grid of workspace cards
4. `/workspaces/[id]` — workspace detail with tabs:
   - **Chat** (default)
   - **Connections**
   - **Documents**
   - **Dashboards**
   - **Usage**
   - **Settings**
5. `/workspaces/[id]/connections/[cid]/schema` — schema explorer
6. `/workspaces/[id]/dashboards/[did]` — dashboard detail
7. `/settings` — user-level settings
8. `/admin/users` — admin only
9. `/admin/audit` — admin only

The current frontend separates `/chat` as its own top-level route;
the redesign could either keep that or fold it into the workspace
detail tab.

---

## 21 — Theme & a11y notes

- Brand voice: technical, calm, "Neural Dark" but a designer is
  free to repaint. Glassmorphism cards are nice but not required.
- Headlines: Space Grotesk (or any geometric sans).
- Body: Inter.
- Code / SQL: JetBrains Mono.
- Multilingual UX (Phase 40 not yet shipped): text bundles for
  `uz`, `ru`, `en`. The agent already mirrors the user's
  language in answers.
- The chat input must support paste / drag-drop for documents
  (POST /documents).
- The chat history sidebar should be virtual-scrollable — long-term
  users accumulate hundreds of sessions.

---

## 22 — Things the redesign should NOT do

- **Never display raw credentials** — the API never returns them
  anyway, but don't add UI surfaces that ask "show password" or
  cache form values in localStorage.
- **Never modify SQL the agent generated**. Show it read-only; the
  user can copy it but can't edit-and-rerun.
- **Don't add a "send raw SQL" power-user mode**. Read-only is a
  defense-in-depth invariant; bypassing the agent's validator at
  the UI layer breaks the security model.
- **Never poll faster than 1Hz**. The Celery jobs settle in
  seconds; aggressive polling thrashes the backend.

---

## 23 — Tech stack the redesign can assume

- Next.js 14 App Router + TypeScript.
- Tailwind CSS available.
- SSE consumed via `fetch` + `ReadableStream` (no need for
  EventSource — Authorization header is required).
- All HTTP goes through `lib/api.ts` — the helper handles auth
  header injection + 401 refresh.
- State per page uses React hooks; no Redux / Zustand currently in.
- Charts: currently Recharts; the redesign can swap (Tremor,
  Visx, etc.) as long as the UISpec contract is honoured.

---

## 24 — One-line for the designer

> **Build a chat-first analytics workspace where users connect any
> of 13 databases / APIs, upload documents, and ask questions in
> their own language; every answer is a typed UISpec ready to
> render as KPI / bar / line / pie / table / dashboard, with
> citations and one-click export.**
