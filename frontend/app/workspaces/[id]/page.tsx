"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { CodeBlock } from "@/components/CodeBlock";
import { GlassPanel } from "@/components/GlassPanel";
import {
  api,
  createConnection,
  deleteConnection,
  getToken,
  listConnections,
  refreshConnection,
  testConnection,
  type ConnectionSummary,
  type Dialect,
  type TestConnectionResult,
} from "@/lib/api";

const DIALECTS: { value: Dialect; label: string; supported: boolean }[] = [
  { value: "postgres", label: "Postgres", supported: true },
  { value: "sqlite", label: "SQLite", supported: true },
  { value: "elasticsearch", label: "Elasticsearch", supported: true },
  { value: "mysql", label: "MySQL", supported: false },
  { value: "clickhouse", label: "ClickHouse", supported: false },
  { value: "oracle", label: "Oracle", supported: false },
  { value: "mongodb", label: "MongoDB", supported: false },
];

const STATUS_TINT: Record<string, string> = {
  pending: "text-on-surface-variant",
  profiling: "text-secondary",
  ready: "text-tertiary",
  error: "text-error",
  auth_error: "text-error",
};

export default function WorkspaceDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const workspaceId = params.id;
  const [workspace, setWorkspace] = useState<{ id: string; name: string } | null>(
    null,
  );
  const [connections, setConnections] = useState<ConnectionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    try {
      const [ws, conns] = await Promise.all([
        api<{ id: string; name: string }>(`/workspaces/${workspaceId}`),
        listConnections(workspaceId),
      ]);
      setWorkspace(ws);
      setConnections(conns);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  }, [workspaceId, router]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onDelete(connId: string) {
    if (!window.confirm("Connection o'chirilsinmi? Schema va RAG chunklari ham yo'qoladi.")) return;
    await deleteConnection(workspaceId, connId);
    await refresh();
  }

  async function onRefresh(connId: string) {
    await refreshConnection(workspaceId, connId);
    await refresh();
  }

  return (
    <main className="mx-auto max-w-4xl px-4 py-8 space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <Link href="/" className="text-on-surface-variant text-sm hover:underline">
            ← Workspaces
          </Link>
          <h1 className="font-headline text-headline-lg text-on-surface mt-2">
            {workspace?.name ?? "Loading…"}
          </h1>
          <p className="text-on-surface-variant text-sm mt-1">
            Connect one or more databases. The agent picks the right one per
            question based on your selection in chat.
          </p>
        </div>
        <div className="flex gap-2">
          <Link
            href={`/chat?workspace=${workspaceId}`}
            className="rounded-xl bg-surface-container-high/60 border border-outline/20 text-on-surface px-4 py-2 font-semibold"
          >
            Open chat
          </Link>
          <button
            type="button"
            onClick={() => setShowForm(true)}
            className="rounded-xl bg-primary-container text-on-primary-container px-4 py-2 font-semibold"
          >
            + Add connection
          </button>
        </div>
      </header>

      {error ? (
        <GlassPanel className="px-4 py-3 text-error text-sm">{error}</GlassPanel>
      ) : null}

      {showForm ? (
        <AddConnectionPanel
          workspaceId={workspaceId}
          onCancel={() => setShowForm(false)}
          onCreated={async () => {
            setShowForm(false);
            await refresh();
          }}
        />
      ) : null}

      {connections === null ? (
        <GlassPanel className="px-5 py-4 text-on-surface-variant">Loading…</GlassPanel>
      ) : connections.length === 0 ? (
        <GlassPanel className="px-5 py-8 text-center">
          <p className="text-on-surface mb-2 font-headline text-xl">
            No connections yet.
          </p>
          <p className="text-on-surface-variant mb-4">
            Add a database connection to start asking questions.
          </p>
          <button
            type="button"
            onClick={() => setShowForm(true)}
            className="inline-block rounded-xl bg-primary-container text-on-primary-container px-4 py-2 font-semibold"
          >
            Add connection
          </button>
        </GlassPanel>
      ) : (
        <div className="space-y-3">
          {connections.map((c) => (
            <GlassPanel key={c.id} className="px-5 py-4">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <div className="font-headline text-on-surface text-lg">
                    {c.name}
                  </div>
                  <div className="text-on-surface-variant text-sm uppercase tracking-wider">
                    {c.dialect}
                  </div>
                </div>
                <span
                  className={
                    "text-xs uppercase tracking-wider " +
                    (STATUS_TINT[c.status] ?? "text-on-surface-variant")
                  }
                >
                  {c.status}
                </span>
              </div>
              <div className="flex flex-wrap gap-3 pt-3 text-sm">
                <Link
                  href={`/workspaces/${workspaceId}/connections/${c.id}/schema`}
                  className="text-primary hover:underline"
                >
                  Schema
                </Link>
                <button
                  type="button"
                  onClick={() => onRefresh(c.id)}
                  className="text-primary hover:underline"
                >
                  Re-profile
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(c.id)}
                  className="text-error hover:underline"
                >
                  Delete
                </button>
              </div>
            </GlassPanel>
          ))}
        </div>
      )}
    </main>
  );
}

// ── Inline add-connection form ────────────────────────────────────

function AddConnectionPanel({
  workspaceId,
  onCancel,
  onCreated,
}: {
  workspaceId: string;
  onCancel: () => void;
  onCreated: () => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [dialect, setDialect] = useState<Dialect>("postgres");
  const [host, setHost] = useState("localhost");
  const [port, setPort] = useState("5432");
  const [dbName, setDbName] = useState("");
  const [path, setPath] = useState("");
  const [ssl, setSsl] = useState(false);
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  // Elasticsearch-specific
  const [esHosts, setEsHosts] = useState("http://localhost:9200");
  const [esAuthMode, setEsAuthMode] = useState<"none" | "apikey" | "basic">(
    "none",
  );
  const [esApiKey, setEsApiKey] = useState("");
  const [esVerifyCerts, setEsVerifyCerts] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(
    null,
  );

  useEffect(() => {
    setTestResult(null);
  }, [
    dialect,
    host,
    port,
    dbName,
    path,
    ssl,
    user,
    password,
    esHosts,
    esAuthMode,
    esApiKey,
    esVerifyCerts,
  ]);

  function buildPayload() {
    if (dialect === "postgres") {
      return {
        name,
        dialect,
        connection_meta: { host, port: Number(port), db_name: dbName, ssl },
        credentials: { user, password },
        auth_kind: "password" as const,
      };
    }
    if (dialect === "sqlite") {
      return {
        name,
        dialect,
        connection_meta: { path },
        credentials: {},
        auth_kind: "none" as const,
      };
    }
    if (dialect === "elasticsearch") {
      const hosts = esHosts
        .split(",")
        .map((h) => h.trim())
        .filter(Boolean);
      const creds: Record<string, string> =
        esAuthMode === "apikey"
          ? { api_key: esApiKey }
          : esAuthMode === "basic"
            ? { user, password }
            : {};
      return {
        name,
        dialect,
        connection_meta: { hosts, verify_certs: esVerifyCerts },
        credentials: creds,
        auth_kind: esAuthMode === "none" ? "none" : "password",
      } as const;
    }
    // Coming dialects — empty for now.
    return {
      name,
      dialect,
      connection_meta: {},
      credentials: {},
      auth_kind: "none" as const,
    };
  }

  async function runTest() {
    setError(null);
    setTesting(true);
    setTestResult(null);
    try {
      // The backend's TestConnectionRequest doesn't accept ``name``
      // (extras forbidden) — strip it before sending.
      const { name: _drop, ...probe } = buildPayload();
      const r = await testConnection(probe);
      setTestResult(r);
    } catch (e) {
      setTestResult({
        ok: false,
        error: e instanceof Error ? e.message : "Test failed",
        error_kind: "other",
      });
    } finally {
      setTesting(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await createConnection(workspaceId, buildPayload());
      await onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create connection");
    } finally {
      setBusy(false);
    }
  }

  const supported =
    DIALECTS.find((d) => d.value === dialect)?.supported ?? false;
  const canCreate = !!name && testResult?.ok === true && supported && !busy;

  return (
    <GlassPanel className="px-5 py-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-headline text-on-surface text-lg">
          Add connection
        </h3>
        <button
          type="button"
          onClick={onCancel}
          className="text-on-surface-variant text-sm hover:text-on-surface"
        >
          ✕
        </button>
      </div>

      <form onSubmit={submit} className="space-y-3">
        <label className="block space-y-1">
          <span className="text-xs uppercase tracking-wider text-on-surface-variant">
            Connection name
          </span>
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="primary-replica"
            className="w-full input"
          />
        </label>

        <div className="space-y-1">
          <span className="text-xs uppercase tracking-wider text-on-surface-variant">
            Dialect
          </span>
          <div className="flex flex-wrap gap-2">
            {DIALECTS.map((d) => (
              <button
                key={d.value}
                type="button"
                onClick={() => setDialect(d.value)}
                className={
                  "px-3 py-1.5 rounded-xl text-sm " +
                  (dialect === d.value
                    ? "bg-primary-container/30 text-primary"
                    : "bg-surface-container-high/40 text-on-surface-variant") +
                  (d.supported ? "" : " opacity-50")
                }
                title={
                  d.supported
                    ? ""
                    : "Coming in Phase 2 — choose Postgres or SQLite for now"
                }
              >
                {d.label}
                {!d.supported ? " (soon)" : ""}
              </button>
            ))}
          </div>
        </div>

        {!supported ? (
          <div className="rounded-xl border border-outline/40 bg-surface-container-high/40 px-3 py-2 text-on-surface-variant text-xs">
            <b>{dialect}</b> engine hozir hali plug qilinmagan. Phase 2'da
            qo'shiladi. Hozir <b>postgres</b> yoki <b>sqlite</b> ni tanlang.
          </div>
        ) : null}

        {dialect === "postgres" ? (
          <>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Host
              </span>
              <input
                required
                value={host}
                onChange={(e) => setHost(e.target.value)}
                className="w-full input"
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Port
                </span>
                <input
                  required
                  type="number"
                  value={port}
                  onChange={(e) => setPort(e.target.value)}
                  className="w-full input"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Database
                </span>
                <input
                  required
                  value={dbName}
                  onChange={(e) => setDbName(e.target.value)}
                  className="w-full input"
                />
              </label>
            </div>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                User
              </span>
              <input
                required
                value={user}
                onChange={(e) => setUser(e.target.value)}
                className="w-full input"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Password
              </span>
              <input
                required
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full input"
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={ssl}
                onChange={(e) => setSsl(e.target.checked)}
              />
              Require TLS (ssl=require)
            </label>
          </>
        ) : dialect === "sqlite" ? (
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wider text-on-surface-variant">
              SQLite file path
            </span>
            <input
              required
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="/var/lib/querymind/sample.db"
              className="w-full input"
            />
          </label>
        ) : dialect === "elasticsearch" ? (
          <>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Hosts (comma-separated URLs)
              </span>
              <input
                required
                value={esHosts}
                onChange={(e) => setEsHosts(e.target.value)}
                placeholder="http://localhost:9200, http://es-2:9200"
                className="w-full input"
              />
            </label>
            <div className="space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Auth
              </span>
              <div className="flex gap-2">
                {(
                  [
                    { v: "none", label: "None" },
                    { v: "apikey", label: "API key" },
                    { v: "basic", label: "User + password" },
                  ] as const
                ).map((m) => (
                  <button
                    key={m.v}
                    type="button"
                    onClick={() => setEsAuthMode(m.v)}
                    className={
                      "px-3 py-1.5 rounded-xl text-sm " +
                      (esAuthMode === m.v
                        ? "bg-primary-container/30 text-primary"
                        : "bg-surface-container-high/40 text-on-surface-variant")
                    }
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>
            {esAuthMode === "apikey" ? (
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  API key
                </span>
                <input
                  required
                  type="password"
                  value={esApiKey}
                  onChange={(e) => setEsApiKey(e.target.value)}
                  placeholder="base64-encoded id:api_key OR raw encoded key"
                  className="w-full input"
                />
              </label>
            ) : null}
            {esAuthMode === "basic" ? (
              <>
                <label className="block space-y-1">
                  <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                    User
                  </span>
                  <input
                    required
                    value={user}
                    onChange={(e) => setUser(e.target.value)}
                    className="w-full input"
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                    Password
                  </span>
                  <input
                    required
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="w-full input"
                  />
                </label>
              </>
            ) : null}
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={esVerifyCerts}
                onChange={(e) => setEsVerifyCerts(e.target.checked)}
              />
              Verify TLS certificates
            </label>
          </>
        ) : null}

        {testing ? (
          <div className="rounded-xl border border-outline/20 bg-surface-container-high/40 px-3 py-2 text-on-surface-variant text-sm">
            Connecting…
          </div>
        ) : testResult ? (
          testResult.ok ? (
            <div className="rounded-xl border border-tertiary/40 bg-tertiary/10 px-3 py-2 text-sm">
              <div className="text-tertiary font-semibold">
                ✓ Connection OK · {testResult.table_count ?? 0} tables
              </div>
              {testResult.table_names_preview &&
              testResult.table_names_preview.length ? (
                <div className="text-on-surface-variant text-xs">
                  {testResult.table_names_preview.join(", ")}
                  {(testResult.table_count ?? 0) >
                  testResult.table_names_preview.length
                    ? ", …"
                    : ""}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="rounded-xl border border-error/40 bg-error/10 px-3 py-2 text-sm">
              <div className="text-error font-semibold">
                ✗ {testResult.error_kind?.toUpperCase()}: {testResult.error}
              </div>
            </div>
          )
        ) : null}

        {error ? <div className="text-error text-sm">{error}</div> : null}

        <div className="flex gap-2 pt-1">
          <button
            type="button"
            onClick={runTest}
            disabled={testing || busy || !supported}
            className="flex-1 rounded-xl bg-surface-container-high/60 border border-outline/20 text-on-surface py-2 font-semibold disabled:opacity-50"
          >
            {testing ? "Testing…" : "Test connection"}
          </button>
          <button
            type="submit"
            disabled={!canCreate}
            title={!canCreate ? "Run a successful Test connection first" : ""}
            className="flex-1 rounded-xl bg-primary-container text-on-primary-container py-2 font-semibold disabled:opacity-50"
          >
            {busy ? "Adding…" : "Add"}
          </button>
        </div>
      </form>
    </GlassPanel>
  );
}
