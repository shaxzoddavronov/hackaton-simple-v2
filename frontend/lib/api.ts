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

export async function registerUser(
  email: string,
  password: string,
): Promise<void> {
  await api<{ id: string; email: string }>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
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
  | "rest_api";

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
};

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
