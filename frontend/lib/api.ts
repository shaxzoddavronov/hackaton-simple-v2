// Tiny fetch + SSE helpers. No external HTTP client — keeps the bundle small.

const TOKEN_KEY = "qm_token";

export function setToken(token: string): void {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(TOKEN_KEY, token);
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function clearToken(): void {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(TOKEN_KEY);
  }
}

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8080";

function authHeader(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export async function api<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeader(),
      ...(init.headers ?? {}),
    },
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${detail}`);
  }
  return (await r.json()) as T;
}

export async function login(
  email: string,
  password: string,
): Promise<string> {
  const body = new URLSearchParams({ username: email, password });
  const r = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!r.ok) throw new Error(`Login failed: ${r.status}`);
  const data = (await r.json()) as { access_token: string };
  setToken(data.access_token);
  return data.access_token;
}

// Phase 16 — public /auth/register is gone. Account creation is
// admin-only through /admin/users. Keeping the export name so the
// /register page that previously imported it still type-checks
// during the legacy import window; the function now throws a
// clear error if a caller still reaches it.
export async function registerUser(): Promise<never> {
  throw new Error(
    "Public registration is disabled. Ask an administrator to create your account.",
  );
}

// ── Phase 16 — admin endpoints (superuser-only) ──────────────────

export type AdminUser = {
  id: string;
  username: string;
  email: string;
  is_superuser: boolean;
  is_active: boolean;
  created_at: string;
};

export type AuditEntry = {
  id: string;
  user_id: string | null;
  action: string;
  target_kind: string | null;
  target_id: string | null;
  status: "ok" | "error" | "denied";
  payload: Record<string, unknown>;
  client_ip: string | null;
  user_agent: string | null;
  created_at: string;
};

export async function listAdminUsers(): Promise<AdminUser[]> {
  return api<AdminUser[]>("/admin/users");
}

export async function createAdminUser(payload: {
  username: string;
  email: string;
  password: string;
  is_superuser?: boolean;
}): Promise<AdminUser> {
  return api<AdminUser>("/admin/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateAdminUser(
  user_id: string,
  payload: Partial<{
    is_active: boolean;
    is_superuser: boolean;
    password: string;
    email: string;
  }>,
): Promise<AdminUser> {
  return api<AdminUser>(`/admin/users/${user_id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteAdminUser(user_id: string): Promise<void> {
  const r = await fetch(`${API_BASE}/admin/users/${user_id}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = (await r.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* response wasn't JSON */
    }
    throw new Error(detail);
  }
}

export async function listAudit(params: {
  user_id?: string;
  action?: string;
  status?: "ok" | "error" | "denied";
  limit?: number;
} = {}): Promise<AuditEntry[]> {
  const qs = new URLSearchParams();
  if (params.user_id) qs.set("user_id", params.user_id);
  if (params.action) qs.set("action", params.action);
  if (params.status) qs.set("status", params.status);
  if (params.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return api<AuditEntry[]>(`/admin/audit${suffix}`);
}

export type TestConnectionResult = {
  ok: boolean;
  dialect?: string | null;
  table_count?: number | null;
  table_names_preview?: string[] | null;
  error?: string | null;
  error_kind?: "auth" | "network" | "timeout" | "config" | "other" | null;
};

export type Dialect =
  | "postgres"
  | "sqlite"
  | "mysql"
  | "clickhouse"
  | "oracle"
  | "mongodb"
  | "elasticsearch"
  | "duckdb"
  | "mssql"
  | "rest_api"
  | "snowflake"
  | "bigquery"
  | "graphql";

export type AuthKind =
  | "password"
  | "dsn"
  | "iam"
  | "none"
  | "bearer"
  | "api_key"
  | "basic"
  | "oauth2_client";

export async function testConnection(payload: {
  dialect: Dialect;
  connection_meta: Record<string, unknown>;
  credentials: Record<string, string>;
  auth_kind: AuthKind;
}): Promise<TestConnectionResult> {
  return api<TestConnectionResult>("/workspaces/test-connection", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ── Connections under a workspace ──

export type ConnectionSummary = {
  id: string;
  workspace_id: string;
  name: string;
  dialect: Dialect;
  status: string;
  profile_job_id?: string | null;
  // Phase 35 — last health-probe outcome. NULL = never probed.
  last_health_check_at?: string | null;
  last_health_ok?: boolean | null;
  last_health_latency_ms?: number | null;
  last_health_error?: string | null;
};

export type ConnectionHealth = {
  connection_id: string;
  dialect: Dialect;
  last_health_check_at: string | null;
  last_health_ok: boolean | null;
  last_health_latency_ms: number | null;
  last_health_error: string | null;
};

export async function getConnectionHealth(
  workspace_id: string,
  connection_id: string,
  refresh = false,
): Promise<ConnectionHealth> {
  const qs = refresh ? "?refresh=true" : "";
  return api<ConnectionHealth>(
    `/workspaces/${workspace_id}/connections/${connection_id}/health${qs}`,
  );
}

export async function listConnections(
  workspace_id: string,
): Promise<ConnectionSummary[]> {
  return api<ConnectionSummary[]>(`/workspaces/${workspace_id}/connections`);
}

export async function createConnection(
  workspace_id: string,
  payload: {
    name: string;
    dialect: Dialect;
    connection_meta: Record<string, unknown>;
    credentials: Record<string, string>;
    auth_kind: AuthKind;
  },
): Promise<ConnectionSummary> {
  return api<ConnectionSummary>(`/workspaces/${workspace_id}/connections`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function deleteConnection(
  workspace_id: string,
  connection_id: string,
): Promise<void> {
  const r = await fetch(
    `${API_BASE}/workspaces/${workspace_id}/connections/${connection_id}`,
    { method: "DELETE", headers: authHeader() },
  );
  if (!r.ok && r.status !== 204) {
    throw new Error(`Delete failed: ${r.status}`);
  }
}

// ── Document sources (Phase 14) ──

export type DocSourceKind =
  | "folder"
  | "url_list"
  | "db_column"
  | "smb"
  | "gdrive"
  | "onedrive"
  | "imap"
  | "slack"
  | "telegram";
export type DocSourceStatus = "idle" | "harvesting" | "ready" | "error";

export type DocSource = {
  id: string;
  workspace_id: string;
  name: string;
  source_kind: DocSourceKind;
  status: DocSourceStatus;
  config: Record<string, unknown>;
  doc_count: number;
  last_harvested_at: string | null;
  last_error: string | null;
};

export async function listDocSources(
  workspace_id: string,
): Promise<DocSource[]> {
  return api<DocSource[]>(`/workspaces/${workspace_id}/doc-sources`);
}

export async function createDocSource(
  workspace_id: string,
  payload: {
    name: string;
    source_kind: DocSourceKind;
    config: Record<string, unknown>;
  },
): Promise<DocSource> {
  return api<DocSource>(`/workspaces/${workspace_id}/doc-sources`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function deleteDocSource(
  workspace_id: string,
  source_id: string,
): Promise<void> {
  const r = await fetch(
    `${API_BASE}/workspaces/${workspace_id}/doc-sources/${source_id}`,
    { method: "DELETE", headers: authHeader() },
  );
  if (!r.ok && r.status !== 204) {
    throw new Error(`Delete failed: ${r.status}`);
  }
}

export async function crawlDocSource(
  workspace_id: string,
  source_id: string,
): Promise<DocSource> {
  return api<DocSource>(
    `/workspaces/${workspace_id}/doc-sources/${source_id}/crawl`,
    { method: "POST" },
  );
}

// ── Dashboards + saved questions (Phase 26 / 27) ──

export type SavedQuestion = {
  id: string;
  workspace_id: string;
  dashboard_id: string | null;
  connection_id: string | null;
  title: string;
  prompt: string;
  position: number | null;
  created_at: string;
};

export type Dashboard = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  question_count: number;
};

export type DashboardDetail = Dashboard & {
  questions: SavedQuestion[];
};

export async function createSavedQuestion(
  workspace_id: string,
  payload: {
    title: string;
    prompt: string;
    dashboard_id?: string | null;
    connection_id?: string | null;
  },
): Promise<SavedQuestion> {
  return api<SavedQuestion>(
    `/workspaces/${workspace_id}/saved-questions`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export async function listSavedQuestions(
  workspace_id: string,
  dashboard_id?: string,
): Promise<SavedQuestion[]> {
  const qs = dashboard_id ? `?dashboard_id=${dashboard_id}` : "";
  return api<SavedQuestion[]>(
    `/workspaces/${workspace_id}/saved-questions${qs}`,
  );
}

export async function deleteSavedQuestion(
  workspace_id: string,
  question_id: string,
): Promise<void> {
  const r = await fetch(
    `${API_BASE}/workspaces/${workspace_id}/saved-questions/${question_id}`,
    { method: "DELETE", headers: authHeader() },
  );
  if (!r.ok && r.status !== 204) {
    throw new Error(`Delete failed: ${r.status}`);
  }
}

export async function updateSavedQuestion(
  workspace_id: string,
  question_id: string,
  payload: { title?: string; dashboard_id?: string | null; position?: number },
): Promise<SavedQuestion> {
  return api<SavedQuestion>(
    `/workspaces/${workspace_id}/saved-questions/${question_id}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

export async function listDashboards(
  workspace_id: string,
): Promise<Dashboard[]> {
  return api<Dashboard[]>(`/workspaces/${workspace_id}/dashboards`);
}

export async function createDashboard(
  workspace_id: string,
  payload: { name: string; description?: string | null },
): Promise<Dashboard> {
  return api<Dashboard>(
    `/workspaces/${workspace_id}/dashboards`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export async function getDashboard(
  workspace_id: string,
  dashboard_id: string,
): Promise<DashboardDetail> {
  return api<DashboardDetail>(
    `/workspaces/${workspace_id}/dashboards/${dashboard_id}`,
  );
}

export async function deleteDashboard(
  workspace_id: string,
  dashboard_id: string,
): Promise<void> {
  const r = await fetch(
    `${API_BASE}/workspaces/${workspace_id}/dashboards/${dashboard_id}`,
    { method: "DELETE", headers: authHeader() },
  );
  if (!r.ok && r.status !== 204) {
    throw new Error(`Delete failed: ${r.status}`);
  }
}

// ── Cloud auth (Phase 18 — OneDrive device-code flow) ──

export type OneDriveStartResponse = {
  device_code: string;
  user_code: string;
  verification_uri: string;
  expires_in: number;
  interval: number;
  message: string;
};

// Discriminated by ``status``; tokens populated only on "ok".
export type OneDrivePollResponse = {
  status:
    | "pending"
    | "slow_down"
    | "expired"
    | "denied"
    | "error"
    | "ok";
  access_token?: string | null;
  refresh_token?: string | null;
  expires_in?: number | null;
  expires_at?: string | null;
  detail?: string | null;
};

export async function onedriveAuthStart(
  client_id: string,
  tenant: string = "common",
): Promise<OneDriveStartResponse> {
  return api<OneDriveStartResponse>("/cloud-auth/onedrive/start", {
    method: "POST",
    body: JSON.stringify({ client_id, tenant }),
  });
}

export async function onedriveAuthPoll(
  client_id: string,
  device_code: string,
  tenant: string = "common",
): Promise<OneDrivePollResponse> {
  return api<OneDrivePollResponse>("/cloud-auth/onedrive/poll", {
    method: "POST",
    body: JSON.stringify({ client_id, device_code, tenant }),
  });
}

export async function uploadDataFile(
  workspace_id: string,
  file: File,
): Promise<ConnectionSummary> {
  const form = new FormData();
  form.append("file", file);
  const r = await fetch(
    `${API_BASE}/workspaces/${workspace_id}/data-files`,
    {
      method: "POST",
      headers: authHeader(),
      body: form,
    },
  );
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${detail}`);
  }
  return (await r.json()) as ConnectionSummary;
}

export async function refreshConnection(
  workspace_id: string,
  connection_id: string,
): Promise<ConnectionSummary> {
  return api<ConnectionSummary>(
    `/workspaces/${workspace_id}/connections/${connection_id}/refresh`,
    { method: "POST" },
  );
}

export type ChatSessionSummary = {
  id: string;
  workspace_id: string;
  title: string;
  created_at: string;
  last_message_at: string;
};

export type StoredMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  ui_spec: unknown;
  created_at: string;
};

export type ChatSessionDetail = {
  session_id: string;
  workspace_id: string | null;
  messages: StoredMessage[];
};

export async function listSessions(
  workspace_id?: string,
): Promise<ChatSessionSummary[]> {
  const qs = workspace_id ? `?workspace_id=${encodeURIComponent(workspace_id)}` : "";
  return api<ChatSessionSummary[]>(`/chat/sessions${qs}`);
}

export async function loadSession(id: string): Promise<ChatSessionDetail> {
  return api<ChatSessionDetail>(`/chat/sessions/${id}`);
}

export async function deleteSession(id: string): Promise<void> {
  const r = await fetch(`${API_BASE}/chat/sessions/${id}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  if (!r.ok && r.status !== 204) {
    throw new Error(`Delete failed: ${r.status}`);
  }
}

export type SseEvent = { event: string; data: unknown };

/**
 * Streams an SSE response from POST /chat. Calls `onEvent` for every parsed
 * `event:`/`data:` pair. Resolves when the server closes the stream.
 */
export async function streamChat(
  payload: {
    message: string;
    session_id?: string | null;
    active_workspace_id?: string | null;
    active_connection_id?: string | null;
  },
  onEvent: (evt: SseEvent) => void,
): Promise<void> {
  const r = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeader(),
    },
    body: JSON.stringify(payload),
  });
  if (!r.ok || !r.body) {
    throw new Error(`Chat stream failed: ${r.status}`);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const lines = chunk.split("\n");
      let evt = "message";
      let data = "";
      for (const line of lines) {
        if (line.startsWith("event:")) evt = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      let parsed: unknown = data;
      try {
        parsed = JSON.parse(data);
      } catch {
        // leave as string
      }
      onEvent({ event: evt, data: parsed });
    }
  }
}


// ── Phase 34 — query result export ──────────────────────────────────

export type ExportFormat = "csv" | "json" | "xlsx";

/** Fetch the cached result rows of one assistant message and trigger
 *  a browser download. Throws Error with the server's reason on 4xx —
 *  in particular, HTTP 410 means the cache was dropped (oversize or
 *  pre-Phase-34 message) and the user must re-ask the question.
 */
export async function downloadMessageExport(
  messageId: string,
  format: ExportFormat,
): Promise<void> {
  const r = await fetch(
    `${API_BASE}/chat/messages/${messageId}/export?format=${format}`,
    { headers: authHeader() },
  );
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = (await r.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* response body wasn't JSON */
    }
    throw new Error(detail);
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  // Server-side Content-Disposition supplies the filename; the
  // download attribute below acts as a fallback for clients that
  // ignore the header (older Safari).
  a.download = `querymind-${messageId.slice(0, 8)}.${format}`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}


// ── Phase 38 — question similarity recall ──────────────────────

export type SimilarQaHit = {
  message_id: string;
  session_id: string;
  question: string;
  headline: string;
  similarity: number;
};


// ── Phase 37 — workspace usage dashboard ──────────────────────────

export type UsageDay = {
  day: string; // ISO date
  llm_calls: number;
  llm_tokens_in: number;
  llm_tokens_out: number;
  queries_ok: number;
  queries_failed: number;
  rag_retrievals: number;
  cache_hits: number;
};

export type UsageTotals = {
  llm_calls: number;
  llm_tokens_in: number;
  llm_tokens_out: number;
  queries_ok: number;
  queries_failed: number;
  rag_retrievals: number;
  cache_hits: number;
};

export type UsageReport = {
  workspace_id: string;
  days: UsageDay[];
  totals: UsageTotals;
};

export async function getWorkspaceUsage(
  workspace_id: string,
  days = 30,
): Promise<UsageReport> {
  return api<UsageReport>(
    `/workspaces/${workspace_id}/usage?days=${days}`,
  );
}
