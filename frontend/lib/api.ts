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

export async function testConnection(payload: {
  dialect: "postgres" | "sqlite";
  connection_meta: Record<string, unknown>;
  credentials: Record<string, string>;
  auth_kind: "password" | "dsn" | "iam" | "none";
}): Promise<TestConnectionResult> {
  return api<TestConnectionResult>("/workspaces/test-connection", {
    method: "POST",
    body: JSON.stringify(payload),
  });
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
