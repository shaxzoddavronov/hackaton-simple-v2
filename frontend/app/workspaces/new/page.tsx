"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { CodeBlock } from "@/components/CodeBlock";
import { GlassPanel } from "@/components/GlassPanel";
import { api, getToken, testConnection, type TestConnectionResult } from "@/lib/api";

type Dialect = "postgres" | "sqlite";

const GRANT_RECIPE: Record<Dialect, string> = {
  postgres: `-- Run as a superuser in the database you want to expose
CREATE ROLE querymind_ro LOGIN PASSWORD 'replace-me';
GRANT CONNECT ON DATABASE your_db TO querymind_ro;
GRANT USAGE  ON SCHEMA   public  TO querymind_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO querymind_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO querymind_ro;`,
  sqlite: `# QueryMind opens SQLite with mode=ro automatically.
# Make sure the file is readable by the backend process,
# and that no other process has it open for write at demo time.`,
};

export default function NewWorkspacePage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [dialect, setDialect] = useState<Dialect>("postgres");
  const [host, setHost] = useState("localhost");
  const [port, setPort] = useState("5432");
  const [dbName, setDbName] = useState("");
  const [path, setPath] = useState("");
  const [ssl, setSsl] = useState(false);
  const [user, setUser] = useState("querymind_ro");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);

  if (typeof window !== "undefined" && !getToken()) {
    router.replace("/login");
  }

  // Editing any connection field invalidates the cached test result —
  // otherwise users could test once with good creds, then sneak in a
  // bad host and click Create. The backend also re-tests, so this is
  // belt + suspenders, but it makes the UI honest.
  useEffect(() => {
    setTestResult(null);
  }, [dialect, host, port, dbName, path, ssl, user, password]);

  function buildPayload() {
    const connection_meta =
      dialect === "postgres"
        ? { host, port: Number(port), db_name: dbName, ssl }
        : { path };
    const credentials =
      dialect === "postgres" ? { user, password } : {};
    const auth_kind = dialect === "postgres" ? "password" : "none";
    return { dialect, connection_meta, credentials, auth_kind } as const;
  }

  async function runTest() {
    setError(null);
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testConnection(buildPayload());
      setTestResult(result);
    } catch (err) {
      setTestResult({
        ok: false,
        error: err instanceof Error ? err.message : "Test request failed",
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
      await api("/workspaces", {
        method: "POST",
        body: JSON.stringify({
          name,
          ...buildPayload(),
        }),
      });
      router.push("/");
    } catch (err) {
      // The backend gates creation on a successful connection probe
      // (workspaces.py::create_workspace) and returns 422 with a
      // structured detail when the probe fails. Surface that here.
      const msg = err instanceof Error ? err.message : "Failed to create workspace";
      const parsed = tryParseConnectionFailure(msg);
      if (parsed) {
        setTestResult({ ok: false, error: parsed.message, error_kind: parsed.kind });
        setError(`Cannot create workspace: ${parsed.message}`);
      } else {
        setError(msg);
      }
    } finally {
      setBusy(false);
    }
  }

  const canCreate = !!name && testResult?.ok === true && !busy && !testing;

  return (
    <main className="mx-auto max-w-3xl px-4 py-8 space-y-6">
      <header>
        <p className="font-mono text-label-caps uppercase text-on-surface-variant">
          New workspace
        </p>
        <h1 className="font-headline text-headline-lg text-on-surface mt-1">
          Connect a database
        </h1>
        <p className="text-on-surface-variant text-sm mt-1">
          QueryMind runs read-only queries against this connection. Use a
          dedicated read-only role for production data.
        </p>
      </header>

      <form onSubmit={submit} className="space-y-4">
        <GlassPanel className="px-5 py-4 space-y-3">
          <Field label="Name">
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Core_Analytics"
              className="w-full input"
            />
          </Field>
          <Field label="Dialect">
            <div className="flex gap-2">
              {(["postgres", "sqlite"] as Dialect[]).map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => setDialect(d)}
                  className={
                    "px-3 py-1.5 rounded-xl text-sm " +
                    (dialect === d
                      ? "bg-primary-container/30 text-primary"
                      : "bg-surface-container-high/40 text-on-surface-variant")
                  }
                >
                  {d}
                </button>
              ))}
            </div>
          </Field>

          {dialect === "postgres" ? (
            <>
              <Field label="Host">
                <input
                  required
                  value={host}
                  onChange={(e) => setHost(e.target.value)}
                  className="w-full input"
                />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Port">
                  <input
                    required
                    type="number"
                    value={port}
                    onChange={(e) => setPort(e.target.value)}
                    className="w-full input"
                  />
                </Field>
                <Field label="Database">
                  <input
                    required
                    value={dbName}
                    onChange={(e) => setDbName(e.target.value)}
                    className="w-full input"
                  />
                </Field>
              </div>
              <Field label="User">
                <input
                  required
                  value={user}
                  onChange={(e) => setUser(e.target.value)}
                  className="w-full input"
                />
              </Field>
              <Field label="Password">
                <input
                  required
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full input"
                />
              </Field>
              <label className="flex items-center gap-2 text-sm text-on-surface-variant">
                <input
                  type="checkbox"
                  checked={ssl}
                  onChange={(e) => setSsl(e.target.checked)}
                />
                Require TLS (ssl=require)
              </label>
            </>
          ) : (
            <Field label="SQLite file path">
              <input
                required
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="/var/lib/querymind/sample.db"
                className="w-full input"
              />
            </Field>
          )}

          <TestConnectionPanel result={testResult} testing={testing} />

          {error ? <div className="text-error text-sm">{error}</div> : null}

          <div className="flex gap-2 pt-1">
            <button
              type="button"
              onClick={runTest}
              disabled={testing || busy}
              className="flex-1 rounded-xl bg-surface-container-high/60 border border-outline/20 text-on-surface py-2 font-semibold disabled:opacity-50"
            >
              {testing ? "Testing…" : "Test connection"}
            </button>
            <button
              type="submit"
              disabled={!canCreate}
              title={
                !testResult?.ok
                  ? "Run a successful Test connection first"
                  : ""
              }
              className="flex-1 rounded-xl bg-primary-container text-on-primary-container py-2 font-semibold disabled:opacity-50"
            >
              {busy ? "Creating…" : "Create workspace"}
            </button>
          </div>
          {!testResult?.ok ? (
            <p className="text-on-surface-variant text-xs">
              Avval <b>Test connection</b> bilan ulanish ishlashiga ishonch hosil
              qiling. Ulanmagan ma&apos;lumotlar bazasi workspace ro&apos;yxatiga
              qo&apos;shilmaydi.
            </p>
          ) : null}
        </GlassPanel>

        <GlassPanel className="px-5 py-4">
          <p className="text-on-surface-variant text-sm mb-2">
            Read-only setup recipe for {dialect}:
          </p>
          <CodeBlock language={dialect} code={GRANT_RECIPE[dialect]} />
        </GlassPanel>
      </form>

      <style jsx>{`
        :global(.input) {
          background: rgba(27, 42, 69, 0.6);
          border: 1px solid rgba(133, 147, 152, 0.2);
          border-radius: 0.75rem;
          padding: 0.5rem 1rem;
          color: #d7e2ff;
          outline: none;
        }
        :global(.input:focus) {
          border-color: #a8e8ff;
        }
      `}</style>
    </main>
  );
}

function TestConnectionPanel({
  result,
  testing,
}: {
  result: TestConnectionResult | null;
  testing: boolean;
}) {
  if (testing) {
    return (
      <div className="rounded-xl border border-outline/20 bg-surface-container-high/40 px-3 py-2 text-on-surface-variant text-sm">
        Connecting…
      </div>
    );
  }
  if (!result) return null;
  if (result.ok) {
    const tables = result.table_names_preview ?? [];
    return (
      <div className="rounded-xl border border-tertiary/40 bg-tertiary/10 px-3 py-2 text-sm space-y-1">
        <div className="text-tertiary font-semibold">
          ✓ Connection OK · {result.table_count ?? 0} tables found
        </div>
        {tables.length ? (
          <div className="text-on-surface-variant text-xs">
            {tables.join(", ")}
            {(result.table_count ?? 0) > tables.length ? ", …" : ""}
          </div>
        ) : (
          <div className="text-on-surface-variant text-xs">
            (No tables found — workspace can still be created, but you may
            have pointed at an empty schema.)
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-error/40 bg-error/10 px-3 py-2 text-sm space-y-1">
      <div className="text-error font-semibold">
        ✗ {(result.error_kind ?? "error").toUpperCase()}: {result.error}
      </div>
      <div className="text-on-surface-variant text-xs">
        {hintFor(result.error_kind)}
      </div>
    </div>
  );
}

function hintFor(kind: TestConnectionResult["error_kind"]): string {
  switch (kind) {
    case "auth":
      return "User/password noto'g'ri yoki rolega kerakli grantlar yo'q.";
    case "network":
      return "Host yoki port noto'g'ri, yoki firewall ulanishni bloklayapti.";
    case "timeout":
      return "Server javob bermadi — VPN, network, yoki DB load tekshiring.";
    case "config":
      return "Connection parametrlari to'liq emas (host/port/db/user/password).";
    default:
      return "DB log'larini tekshiring.";
  }
}

function tryParseConnectionFailure(
  message: string,
): { kind: TestConnectionResult["error_kind"]; message: string } | null {
  // FastAPI returns errors as "422 ... : {"detail":{"code":"connection_auth","message":"..."}}"
  const idx = message.indexOf("{");
  if (idx < 0) return null;
  try {
    const payload = JSON.parse(message.slice(idx)) as {
      detail?: { code?: string; message?: string };
    };
    const detail = payload.detail;
    if (!detail) return null;
    const kind = (detail.code || "")
      .replace(/^connection_/, "") as TestConnectionResult["error_kind"];
    return { kind: kind || "other", message: detail.message || "Connection failed" };
  } catch {
    return null;
  }
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs uppercase tracking-wider text-on-surface-variant">
        {label}
      </span>
      {children}
    </label>
  );
}
