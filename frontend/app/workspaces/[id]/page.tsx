"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { CodeBlock } from "@/components/CodeBlock";
import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
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
  { value: "mysql", label: "MySQL", supported: true },
  { value: "clickhouse", label: "ClickHouse", supported: true },
  { value: "oracle", label: "Oracle", supported: true },
  { value: "mongodb", label: "MongoDB", supported: true },
  { value: "elasticsearch", label: "Elasticsearch", supported: true },
  { value: "duckdb", label: "DuckDB", supported: true },
  { value: "mssql", label: "SQL Server", supported: true },
  { value: "rest_api", label: "REST API / CRM / 1C", supported: true },
];

const REST_PRESETS: { value: string; label: string }[] = [
  { value: "generic", label: "Generic (no preset)" },
  { value: "bitrix24", label: "Bitrix24 CRM" },
  { value: "amocrm", label: "AmoCRM v4" },
  { value: "odata_1c", label: "1C OData" },
  { value: "hubspot", label: "HubSpot CRM v3" },
  { value: "salesforce", label: "Salesforce REST" },
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
  const toast = useToast();
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
    try {
      await deleteConnection(workspaceId, connId);
      toast.success("Connection removed");
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to delete connection");
    }
  }

  async function onRefresh(connId: string) {
    try {
      await refreshConnection(workspaceId, connId);
      toast.info("Re-profiling started");
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to refresh connection");
    }
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
  // MongoDB-specific
  const [mongoAuth, setMongoAuth] = useState<"none" | "password">("none");
  const [mongoTls, setMongoTls] = useState(false);
  const [mongoReplicaSet, setMongoReplicaSet] = useState("");
  // REST API specific
  const [apiBaseUrl, setApiBaseUrl] = useState("https://");
  const [apiSpecSource, setApiSpecSource] = useState<
    "preset" | "openapi_url" | "openapi_file" | "none"
  >("preset");
  const [apiPreset, setApiPreset] = useState("generic");
  const [apiSpecUrl, setApiSpecUrl] = useState("");
  const [apiSpecB64, setApiSpecB64] = useState("");
  const [apiAuthKind, setApiAuthKind] = useState<
    "bearer" | "api_key" | "basic" | "oauth2_client" | "none"
  >("bearer");
  const [apiToken, setApiToken] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiKeyLocation, setApiKeyLocation] = useState<"header" | "query">("header");
  const [apiKeyName, setApiKeyName] = useState("X-API-Key");
  const [apiClientId, setApiClientId] = useState("");
  const [apiClientSecret, setApiClientSecret] = useState("");
  const [apiTokenUrl, setApiTokenUrl] = useState("");
  const [apiScope, setApiScope] = useState("");
  const [apiTimeoutS, setApiTimeoutS] = useState("30");
  const toast = useToast();
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
    mongoAuth,
    mongoTls,
    mongoReplicaSet,
  ]);

  // When the user picks a new SQL-ish dialect, snap the port to that
  // dialect's well-known default so the form doesn't keep showing the
  // previous dialect's port (e.g. 5432 lingering after switching to
  // MySQL).
  useEffect(() => {
    const defaults: Partial<Record<Dialect, string>> = {
      postgres: "5432",
      mysql: "3306",
      clickhouse: "8123",
      oracle: "1521",
      mongodb: "27017",
      mssql: "1433",
    };
    const next = defaults[dialect];
    if (next !== undefined) {
      setPort(next);
    }
    if (dialect === "clickhouse" && !user) {
      setUser("default");
    }
  }, [dialect]); // eslint-disable-line react-hooks/exhaustive-deps

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
      const creds: Record<string, string> = {};
      return {
        name,
        dialect,
        connection_meta: { path },
        credentials: creds,
        auth_kind: "none" as const,
      };
    }
    if (dialect === "duckdb") {
      const creds: Record<string, string> = {};
      return {
        name,
        dialect,
        connection_meta: { path: path || ":memory:" },
        credentials: creds,
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
    if (dialect === "mysql") {
      return {
        name,
        dialect,
        connection_meta: { host, port: Number(port), db_name: dbName, ssl },
        credentials: { user, password },
        auth_kind: "password" as const,
      };
    }
    if (dialect === "clickhouse") {
      return {
        name,
        dialect,
        connection_meta: { host, port: Number(port), db_name: dbName, ssl },
        credentials: { user, password },
        auth_kind: "password" as const,
      };
    }
    if (dialect === "oracle") {
      return {
        name,
        dialect,
        connection_meta: { host, port: Number(port), db_name: dbName },
        credentials: { user, password },
        auth_kind: "password" as const,
      };
    }
    if (dialect === "mssql") {
      return {
        name,
        dialect,
        connection_meta: {
          host,
          port: Number(port) || 1433,
          db_name: dbName,
        },
        credentials: { user, password },
        auth_kind: "password" as const,
      };
    }
    if (dialect === "mongodb") {
      const meta: Record<string, unknown> = {
        host,
        port: Number(port),
        db_name: dbName,
        tls: mongoTls,
      };
      if (mongoReplicaSet) {
        meta.replica_set = mongoReplicaSet;
      }
      const creds: Record<string, string> =
        mongoAuth === "password" ? { user, password } : {};
      return {
        name,
        dialect,
        connection_meta: meta,
        credentials: creds,
        auth_kind: mongoAuth === "password" ? "password" : "none",
      } as const;
    }
    if (dialect === "rest_api") {
      const meta: Record<string, unknown> = {
        base_url: apiBaseUrl.replace(/\/$/, ""),
        spec_source: apiSpecSource,
        timeout_s: Number(apiTimeoutS) || 30,
      };
      if (apiSpecSource === "preset") {
        meta.preset = apiPreset;
      } else if (apiSpecSource === "openapi_url") {
        meta.spec_url = apiSpecUrl;
      } else if (apiSpecSource === "openapi_file" && apiSpecB64) {
        meta.spec_content_b64 = apiSpecB64;
      }
      const credentials: Record<string, string> = {};
      if (apiAuthKind === "bearer") {
        credentials.token = apiToken;
      } else if (apiAuthKind === "api_key") {
        credentials.key = apiKey;
        credentials.key_location = apiKeyLocation;
        credentials.key_name = apiKeyName;
      } else if (apiAuthKind === "basic") {
        credentials.username = user;
        credentials.password = password;
      } else if (apiAuthKind === "oauth2_client") {
        credentials.client_id = apiClientId;
        credentials.client_secret = apiClientSecret;
        credentials.token_url = apiTokenUrl;
        if (apiScope) credentials.scope = apiScope;
      }
      return {
        name,
        dialect,
        connection_meta: meta,
        credentials,
        auth_kind: apiAuthKind,
      } as const;
    }
    // Fallback — should not be reachable now that every dialect is supported.
    const emptyMeta: Record<string, unknown> = {};
    const emptyCreds: Record<string, string> = {};
    return {
      name,
      dialect,
      connection_meta: emptyMeta,
      credentials: emptyCreds,
      auth_kind: "none" as const,
    };
  }

  async function runTest() {
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
    setBusy(true);
    try {
      await createConnection(workspaceId, buildPayload());
      toast.success("Connection added — profiling started");
      await onCreated();
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to create connection",
      );
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
            <b>{dialect}</b> engine hozir hali plug qilinmagan.
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
        ) : dialect === "duckdb" ? (
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wider text-on-surface-variant">
              DuckDB file path
            </span>
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder=":memory:"
              className="w-full input"
            />
            <span className="text-xs text-on-surface-variant">
              Defaults to <code>:memory:</code> when left blank. On-disk
              files are opened read-only.
            </span>
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
        ) : dialect === "mysql" ||
          dialect === "clickhouse" ||
          dialect === "oracle" ||
          dialect === "mssql" ? (
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
                  {dialect === "oracle" ? "Service name" : "Database"}
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
                required={dialect !== "clickhouse"}
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
                required={dialect !== "clickhouse"}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full input"
              />
            </label>
            {dialect === "mysql" ? (
              <label className="flex items-center gap-2 text-sm text-on-surface-variant">
                <input
                  type="checkbox"
                  checked={ssl}
                  onChange={(e) => setSsl(e.target.checked)}
                />
                Require TLS
              </label>
            ) : null}
            {dialect === "clickhouse" ? (
              <label className="flex items-center gap-2 text-sm text-on-surface-variant">
                <input
                  type="checkbox"
                  checked={ssl}
                  onChange={(e) => setSsl(e.target.checked)}
                />
                Use HTTPS
              </label>
            ) : null}
          </>
        ) : dialect === "mongodb" ? (
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
            <div className="space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Auth
              </span>
              <div className="flex gap-2">
                {(
                  [
                    { v: "none", label: "None" },
                    { v: "password", label: "User + password" },
                  ] as const
                ).map((m) => (
                  <button
                    key={m.v}
                    type="button"
                    onClick={() => setMongoAuth(m.v)}
                    className={
                      "px-3 py-1.5 rounded-xl text-sm " +
                      (mongoAuth === m.v
                        ? "bg-primary-container/30 text-primary"
                        : "bg-surface-container-high/40 text-on-surface-variant")
                    }
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>
            {mongoAuth === "password" ? (
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
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Replica set (optional)
              </span>
              <input
                value={mongoReplicaSet}
                onChange={(e) => setMongoReplicaSet(e.target.value)}
                placeholder="rs0"
                className="w-full input"
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={mongoTls}
                onChange={(e) => setMongoTls(e.target.checked)}
              />
              Use TLS
            </label>
          </>
        ) : dialect === "rest_api" ? (
          <>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Base URL
              </span>
              <input
                required
                value={apiBaseUrl}
                onChange={(e) => setApiBaseUrl(e.target.value)}
                placeholder="https://api.example.com"
                className="w-full input"
              />
            </label>

            <div className="space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Spec source
              </span>
              <div className="flex flex-wrap gap-2">
                {(
                  [
                    { v: "preset", label: "Preset (CRM/ERP)" },
                    { v: "openapi_url", label: "OpenAPI URL" },
                    { v: "openapi_file", label: "Upload spec" },
                    { v: "none", label: "Custom (no spec)" },
                  ] as const
                ).map((m) => (
                  <button
                    key={m.v}
                    type="button"
                    onClick={() => setApiSpecSource(m.v)}
                    className={
                      "px-3 py-1.5 rounded-xl text-sm " +
                      (apiSpecSource === m.v
                        ? "bg-primary-container/30 text-primary"
                        : "bg-surface-container-high/40 text-on-surface-variant")
                    }
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>

            {apiSpecSource === "preset" ? (
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Preset
                </span>
                <select
                  value={apiPreset}
                  onChange={(e) => setApiPreset(e.target.value)}
                  className="w-full input"
                >
                  {REST_PRESETS.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}

            {apiSpecSource === "openapi_url" ? (
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  OpenAPI spec URL
                </span>
                <input
                  required
                  value={apiSpecUrl}
                  onChange={(e) => setApiSpecUrl(e.target.value)}
                  placeholder="https://api.example.com/openapi.json"
                  className="w-full input"
                />
              </label>
            ) : null}

            {apiSpecSource === "openapi_file" ? (
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Upload OpenAPI JSON
                </span>
                <input
                  type="file"
                  accept=".json,application/json"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (!f) {
                      setApiSpecB64("");
                      return;
                    }
                    const buf = await f.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let s = "";
                    for (let i = 0; i < bytes.length; i++) {
                      s += String.fromCharCode(bytes[i] as number);
                    }
                    setApiSpecB64(btoa(s));
                  }}
                  className="w-full input"
                />
                {apiSpecB64 ? (
                  <span className="text-xs text-tertiary">
                    Loaded ({Math.round((apiSpecB64.length * 3) / 4)} bytes)
                  </span>
                ) : null}
              </label>
            ) : null}

            <div className="space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Authentication
              </span>
              <div className="flex flex-wrap gap-2">
                {(
                  [
                    { v: "bearer", label: "Bearer token" },
                    { v: "api_key", label: "API key" },
                    { v: "basic", label: "Basic auth" },
                    { v: "oauth2_client", label: "OAuth2 client" },
                    { v: "none", label: "None" },
                  ] as const
                ).map((m) => (
                  <button
                    key={m.v}
                    type="button"
                    onClick={() => setApiAuthKind(m.v)}
                    className={
                      "px-3 py-1.5 rounded-xl text-sm " +
                      (apiAuthKind === m.v
                        ? "bg-primary-container/30 text-primary"
                        : "bg-surface-container-high/40 text-on-surface-variant")
                    }
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>

            {apiAuthKind === "bearer" ? (
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Bearer token
                </span>
                <input
                  required
                  type="password"
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                  className="w-full input"
                />
              </label>
            ) : null}

            {apiAuthKind === "api_key" ? (
              <>
                <label className="block space-y-1">
                  <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                    API key value
                  </span>
                  <input
                    required
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    className="w-full input"
                  />
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <label className="block space-y-1">
                    <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                      Location
                    </span>
                    <select
                      value={apiKeyLocation}
                      onChange={(e) =>
                        setApiKeyLocation(e.target.value as "header" | "query")
                      }
                      className="w-full input"
                    >
                      <option value="header">Header</option>
                      <option value="query">Query param</option>
                    </select>
                  </label>
                  <label className="block space-y-1">
                    <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                      Param/header name
                    </span>
                    <input
                      required
                      value={apiKeyName}
                      onChange={(e) => setApiKeyName(e.target.value)}
                      className="w-full input"
                    />
                  </label>
                </div>
              </>
            ) : null}

            {apiAuthKind === "basic" ? (
              <>
                <label className="block space-y-1">
                  <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                    Username
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

            {apiAuthKind === "oauth2_client" ? (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <label className="block space-y-1">
                    <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                      Client ID
                    </span>
                    <input
                      required
                      value={apiClientId}
                      onChange={(e) => setApiClientId(e.target.value)}
                      className="w-full input"
                    />
                  </label>
                  <label className="block space-y-1">
                    <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                      Client secret
                    </span>
                    <input
                      required
                      type="password"
                      value={apiClientSecret}
                      onChange={(e) => setApiClientSecret(e.target.value)}
                      className="w-full input"
                    />
                  </label>
                </div>
                <label className="block space-y-1">
                  <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                    Token URL
                  </span>
                  <input
                    required
                    value={apiTokenUrl}
                    onChange={(e) => setApiTokenUrl(e.target.value)}
                    placeholder="https://login.example.com/oauth2/token"
                    className="w-full input"
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                    Scope (optional)
                  </span>
                  <input
                    value={apiScope}
                    onChange={(e) => setApiScope(e.target.value)}
                    className="w-full input"
                  />
                </label>
              </>
            ) : null}

            {apiAuthKind === "none" ? (
              <div className="rounded-xl border border-outline/20 bg-surface-container-high/40 px-3 py-2 text-on-surface-variant text-xs">
                Public API — no credentials sent.
              </div>
            ) : null}

            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Request timeout (seconds)
              </span>
              <input
                type="number"
                min={1}
                max={300}
                value={apiTimeoutS}
                onChange={(e) => setApiTimeoutS(e.target.value)}
                className="w-full input"
              />
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
