"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { CodeBlock } from "@/components/CodeBlock";
import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
import {
  api,
  crawlDocSource,
  createConnection,
  createDocSource,
  deleteConnection,
  deleteDocSource,
  getToken,
  listConnections,
  listDocSources,
  onedriveAuthPoll,
  onedriveAuthStart,
  refreshConnection,
  testConnection,
  uploadDataFile,
  type ConnectionSummary,
  type Dialect,
  type DocSource,
  type DocSourceKind,
  type OneDriveStartResponse,
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

  async function onUploadFile(file: File) {
    try {
      await uploadDataFile(workspaceId, file);
      toast.success(`${file.name} uploaded — profiling started`);
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Upload failed");
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
          <label className="rounded-xl bg-surface-container-high/60 border border-outline/20 text-on-surface px-4 py-2 font-semibold cursor-pointer">
            Upload data file
            <input
              type="file"
              accept=".csv,.tsv,.parquet,.pq,.json,.ndjson,.jsonl"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.target.value = "";
                if (f) void onUploadFile(f);
              }}
            />
          </label>
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

      <DocSourcesSection workspaceId={workspaceId} />
    </main>
  );
}

// ── Knowledge sources section ─────────────────────────────────────

const DOC_SOURCE_KIND_LABEL: Record<DocSourceKind, string> = {
  folder: "Folder",
  url_list: "URL list",
  db_column: "DB column",
  smb: "SMB share",
  gdrive: "Google Drive",
  onedrive: "OneDrive",
  imap: "Email (IMAP)",
};

function DocSourcesSection({ workspaceId }: { workspaceId: string }) {
  const toast = useToast();
  const [sources, setSources] = useState<DocSource[] | null>(null);
  const [showForm, setShowForm] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const rows = await listDocSources(workspaceId);
      setSources(rows);
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to load doc sources",
      );
    }
  }, [workspaceId, toast]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onCrawl(id: string) {
    try {
      await crawlDocSource(workspaceId, id);
      toast.info("Crawl started");
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to crawl");
    }
  }

  async function onDelete(id: string) {
    if (
      !window.confirm(
        "Bu manba va undagi barcha hujjat chunki o'chiriladi. Davom etilsinmi?",
      )
    ) {
      return;
    }
    try {
      await deleteDocSource(workspaceId, id);
      toast.success("Source removed");
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to delete");
    }
  }

  return (
    <section className="space-y-3 pt-4">
      <div className="flex items-end justify-between">
        <div>
          <h2 className="font-headline text-headline-md text-on-surface">
            Knowledge sources
          </h2>
          <p className="text-on-surface-variant text-sm">
            Harvest PDFs, Office docs and HTML from folders, URLs or
            DB-column file references. Indexed in Uzbek, Russian and
            English via bge-m3 — the agent retrieves the most relevant
            files regardless of question language.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowForm(true)}
          className="rounded-xl bg-surface-container-high/60 border border-outline/20 text-on-surface px-3 py-1.5 font-semibold"
        >
          + Add source
        </button>
      </div>

      {showForm ? (
        <AddDocSourcePanel
          workspaceId={workspaceId}
          onCancel={() => setShowForm(false)}
          onCreated={async () => {
            setShowForm(false);
            await refresh();
          }}
        />
      ) : null}

      {sources === null ? (
        <GlassPanel className="px-5 py-3 text-on-surface-variant text-sm">
          Loading…
        </GlassPanel>
      ) : sources.length === 0 ? (
        <GlassPanel className="px-5 py-4 text-on-surface-variant text-sm">
          No knowledge sources yet. Add one to ingest documents into RAG.
        </GlassPanel>
      ) : (
        <div className="space-y-2">
          {sources.map((s) => (
            <GlassPanel key={s.id} className="px-5 py-3">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <div className="font-headline text-on-surface text-base">
                    {s.name}
                  </div>
                  <div className="text-on-surface-variant text-xs uppercase tracking-wider">
                    {DOC_SOURCE_KIND_LABEL[s.source_kind] ?? s.source_kind}
                    {" · "}
                    {s.doc_count} docs
                    {s.last_harvested_at
                      ? " · last " +
                        new Date(s.last_harvested_at).toLocaleString()
                      : ""}
                  </div>
                  {s.last_error ? (
                    <div className="text-error text-xs mt-1">
                      {s.last_error}
                    </div>
                  ) : null}
                </div>
                <span
                  className={
                    "text-xs uppercase tracking-wider " +
                    (STATUS_TINT[s.status] ?? "text-on-surface-variant")
                  }
                >
                  {s.status}
                </span>
              </div>
              <div className="flex gap-3 pt-2 text-sm">
                <button
                  type="button"
                  onClick={() => onCrawl(s.id)}
                  className="text-primary hover:underline"
                >
                  Crawl now
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(s.id)}
                  className="text-error hover:underline"
                >
                  Delete
                </button>
              </div>
            </GlassPanel>
          ))}
        </div>
      )}
    </section>
  );
}

function AddDocSourcePanel({
  workspaceId,
  onCancel,
  onCreated,
}: {
  workspaceId: string;
  onCancel: () => void;
  onCreated: () => Promise<void>;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<DocSourceKind>("folder");
  // folder
  const [folderPath, setFolderPath] = useState("");
  const [folderRecursive, setFolderRecursive] = useState(true);
  // url_list
  const [urlListText, setUrlListText] = useState("");
  // db_column
  const [dbConnId, setDbConnId] = useState("");
  const [dbTable, setDbTable] = useState("");
  const [dbColumn, setDbColumn] = useState("");
  const [dbUrlPrefix, setDbUrlPrefix] = useState("");
  const [dbExtraColumns, setDbExtraColumns] = useState("");
  // smb
  const [smbServer, setSmbServer] = useState("");
  const [smbShare, setSmbShare] = useState("");
  const [smbPath, setSmbPath] = useState("");
  const [smbUsername, setSmbUsername] = useState("");
  const [smbPassword, setSmbPassword] = useState("");
  const [smbDomain, setSmbDomain] = useState("");
  const [smbRecursive, setSmbRecursive] = useState(true);
  // gdrive
  const [gdriveFolderId, setGdriveFolderId] = useState("");
  const [gdriveServiceJson, setGdriveServiceJson] = useState("");
  const [gdriveRecursive, setGdriveRecursive] = useState(true);
  // onedrive
  const [oneDriveAccessToken, setOneDriveAccessToken] = useState("");
  const [oneDriveRefreshToken, setOneDriveRefreshToken] = useState("");
  const [oneDriveClientId, setOneDriveClientId] = useState("");
  const [oneDriveTenant, setOneDriveTenant] = useState("common");
  const [oneDriveFolderPath, setOneDriveFolderPath] = useState("/");
  const [oneDriveDriveId, setOneDriveDriveId] = useState("");
  // OneDrive device-flow state (Phase 18). When ``oneDriveDeviceFlow``
  // is non-null, the form shows the verification_uri + user_code and
  // polls the backend every ``interval`` seconds until tokens arrive.
  const [oneDriveDeviceFlow, setOneDriveDeviceFlow] =
    useState<OneDriveStartResponse | null>(null);
  const [oneDriveAuthStatus, setOneDriveAuthStatus] = useState<string | null>(
    null,
  );
  const [oneDriveAuthBusy, setOneDriveAuthBusy] = useState(false);
  // imap (Phase 19)
  const [imapServer, setImapServer] = useState("");
  const [imapPort, setImapPort] = useState("993");
  const [imapSsl, setImapSsl] = useState(true);
  const [imapUsername, setImapUsername] = useState("");
  const [imapPassword, setImapPassword] = useState("");
  const [imapFolder, setImapFolder] = useState("INBOX");
  const [imapSinceDays, setImapSinceDays] = useState("90");
  const [imapMaxMessages, setImapMaxMessages] = useState("500");
  const [imapIncludeAttachments, setImapIncludeAttachments] = useState(true);
  const [busy, setBusy] = useState(false);

  async function runOneDriveAuth() {
    if (!oneDriveClientId.trim()) {
      toast.error("Client ID required to start OneDrive auth");
      return;
    }
    setOneDriveAuthBusy(true);
    setOneDriveAuthStatus("Starting device flow…");
    try {
      const startResp = await onedriveAuthStart(
        oneDriveClientId,
        oneDriveTenant || "common",
      );
      setOneDriveDeviceFlow(startResp);
      setOneDriveAuthStatus(
        `Open ${startResp.verification_uri} and enter code: ${startResp.user_code}`,
      );

      // Poll loop. MS spec: respect the server-given interval and bump
      // by 5 s when we see "slow_down".
      const deadline = Date.now() + startResp.expires_in * 1000;
      let interval = Math.max(startResp.interval, 1) * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, interval));
        const poll = await onedriveAuthPoll(
          oneDriveClientId,
          startResp.device_code,
          oneDriveTenant || "common",
        );
        if (poll.status === "ok") {
          setOneDriveAccessToken(poll.access_token || "");
          setOneDriveRefreshToken(poll.refresh_token || "");
          setOneDriveDeviceFlow(null);
          setOneDriveAuthStatus(
            "Authorised — tokens populated below. Click 'Create source' to save.",
          );
          toast.success("OneDrive authorised");
          return;
        }
        if (poll.status === "pending") {
          continue;
        }
        if (poll.status === "slow_down") {
          interval += 5000;
          continue;
        }
        // expired / denied / error are terminal.
        setOneDriveAuthStatus(
          `Auth ${poll.status}: ${poll.detail ?? "no detail"}`,
        );
        setOneDriveDeviceFlow(null);
        toast.error(`OneDrive auth ${poll.status}`);
        return;
      }
      setOneDriveAuthStatus("Device code expired — restart the flow.");
      setOneDriveDeviceFlow(null);
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "OneDrive auth failed",
      );
      setOneDriveAuthStatus(null);
      setOneDriveDeviceFlow(null);
    } finally {
      setOneDriveAuthBusy(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      let config: Record<string, unknown> = {};
      if (kind === "folder") {
        config = { path: folderPath, recursive: folderRecursive };
      } else if (kind === "url_list") {
        const urls = urlListText
          .split(/\r?\n/)
          .map((u) => u.trim())
          .filter(Boolean);
        if (urls.length === 0) {
          toast.error("URL list cannot be empty");
          setBusy(false);
          return;
        }
        config = { urls };
      } else if (kind === "db_column") {
        // Comma-separated extras (Phase 17.1) — pulled alongside the
        // PK + file column so chunk_metadata carries the row's
        // human-readable identifiers ("title", "created_at", etc.).
        const extras = dbExtraColumns
          .split(/[,\n]/)
          .map((s) => s.trim())
          .filter(Boolean);
        config = {
          connection_id: dbConnId,
          table: dbTable,
          column: dbColumn,
          ...(dbUrlPrefix ? { url_prefix: dbUrlPrefix } : {}),
          ...(extras.length ? { extra_columns: extras } : {}),
        };
      } else if (kind === "smb") {
        config = {
          server: smbServer,
          share: smbShare,
          ...(smbPath ? { path: smbPath } : {}),
          username: smbUsername,
          password: smbPassword,
          ...(smbDomain ? { domain: smbDomain } : {}),
          recursive: smbRecursive,
        };
      } else if (kind === "gdrive") {
        if (!gdriveServiceJson.trim()) {
          toast.error("Service-account JSON cannot be empty");
          setBusy(false);
          return;
        }
        // Validate JSON shape client-side so the user notices typos
        // before the backend rejects with a generic 400.
        try {
          JSON.parse(gdriveServiceJson);
        } catch {
          toast.error("Service-account JSON is not valid JSON");
          setBusy(false);
          return;
        }
        config = {
          folder_id: gdriveFolderId,
          service_account_json: gdriveServiceJson,
          recursive: gdriveRecursive,
        };
      } else if (kind === "onedrive") {
        config = {
          access_token: oneDriveAccessToken,
          client_id: oneDriveClientId,
          ...(oneDriveRefreshToken
            ? { refresh_token: oneDriveRefreshToken }
            : {}),
          tenant: oneDriveTenant || "common",
          folder_path: oneDriveFolderPath || "/",
          ...(oneDriveDriveId ? { drive_id: oneDriveDriveId } : {}),
        };
      } else if (kind === "imap") {
        config = {
          server: imapServer,
          port: Number(imapPort) || 993,
          ssl: imapSsl,
          username: imapUsername,
          password: imapPassword,
          folder: imapFolder || "INBOX",
          since_days: Number(imapSinceDays) || 90,
          max_messages: Number(imapMaxMessages) || 500,
          include_attachments: imapIncludeAttachments,
        };
      }
      await createDocSource(workspaceId, {
        name,
        source_kind: kind,
        config,
      });
      toast.success("Source created — click 'Crawl now' to ingest");
      await onCreated();
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to create source",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <GlassPanel className="px-5 py-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-headline text-on-surface text-base">
          Add knowledge source
        </h3>
        <button
          type="button"
          onClick={onCancel}
          className="text-on-surface-variant text-sm hover:text-on-surface"
        >
          Cancel
        </button>
      </div>
      <form onSubmit={submit} className="space-y-3">
        <label className="block space-y-1">
          <span className="text-xs uppercase tracking-wider text-on-surface-variant">
            Name
          </span>
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="hr-handbook"
            className="w-full input"
          />
        </label>

        <div className="space-y-1">
          <span className="text-xs uppercase tracking-wider text-on-surface-variant">
            Source kind
          </span>
          <div className="flex flex-wrap gap-2">
            {(
              [
                { v: "folder", label: "Folder" },
                { v: "url_list", label: "URL list" },
                { v: "db_column", label: "DB column" },
                { v: "smb", label: "SMB share" },
                { v: "gdrive", label: "Google Drive" },
                { v: "onedrive", label: "OneDrive" },
                { v: "imap", label: "Email (IMAP)" },
              ] as const
            ).map((m) => (
              <button
                key={m.v}
                type="button"
                onClick={() => setKind(m.v)}
                className={
                  "px-3 py-1.5 rounded-xl text-sm " +
                  (kind === m.v
                    ? "bg-primary-container/30 text-primary"
                    : "bg-surface-container-high/40 text-on-surface-variant")
                }
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>

        {kind === "folder" ? (
          <>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Folder path (server-local or mounted)
              </span>
              <input
                required
                value={folderPath}
                onChange={(e) => setFolderPath(e.target.value)}
                placeholder="/data/docs"
                className="w-full input"
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={folderRecursive}
                onChange={(e) => setFolderRecursive(e.target.checked)}
              />
              Recurse into subfolders
            </label>
          </>
        ) : null}

        {kind === "url_list" ? (
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wider text-on-surface-variant">
              URLs (one per line, https only)
            </span>
            <textarea
              required
              value={urlListText}
              onChange={(e) => setUrlListText(e.target.value)}
              placeholder="https://example.com/docs/policy.pdf"
              rows={5}
              className="w-full input font-mono text-xs"
            />
          </label>
        ) : null}

        {kind === "db_column" ? (
          <>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Connection UUID
              </span>
              <input
                required
                value={dbConnId}
                onChange={(e) => setDbConnId(e.target.value)}
                placeholder="<copy from a connection row above>"
                className="w-full input font-mono text-xs"
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Table
                </span>
                <input
                  required
                  value={dbTable}
                  onChange={(e) => setDbTable(e.target.value)}
                  className="w-full input"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Column with file path/URL
                </span>
                <input
                  required
                  value={dbColumn}
                  onChange={(e) => setDbColumn(e.target.value)}
                  className="w-full input"
                />
              </label>
            </div>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                URL prefix (optional — prepended to relative values)
              </span>
              <input
                value={dbUrlPrefix}
                onChange={(e) => setDbUrlPrefix(e.target.value)}
                placeholder="https://files.example.com"
                className="w-full input"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Extra columns (optional — comma-separated, e.g.
                title, created_at)
              </span>
              <input
                value={dbExtraColumns}
                onChange={(e) => setDbExtraColumns(e.target.value)}
                placeholder="title, created_at"
                className="w-full input"
              />
              <span className="text-xs text-on-surface-variant">
                Each row&apos;s values for these columns travel with
                the file&apos;s RAG chunks so citations can reference
                the originating row by its human-readable identifiers.
              </span>
            </label>
          </>
        ) : null}

        {kind === "smb" ? (
          <>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Server
                </span>
                <input
                  required
                  value={smbServer}
                  onChange={(e) => setSmbServer(e.target.value)}
                  placeholder="fileserver.local or 10.0.0.42"
                  className="w-full input"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Share
                </span>
                <input
                  required
                  value={smbShare}
                  onChange={(e) => setSmbShare(e.target.value)}
                  placeholder="Documents"
                  className="w-full input"
                />
              </label>
            </div>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Path inside share (optional)
              </span>
              <input
                value={smbPath}
                onChange={(e) => setSmbPath(e.target.value)}
                placeholder="HR/Policies"
                className="w-full input"
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Username
                </span>
                <input
                  required
                  value={smbUsername}
                  onChange={(e) => setSmbUsername(e.target.value)}
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
                  value={smbPassword}
                  onChange={(e) => setSmbPassword(e.target.value)}
                  className="w-full input"
                />
              </label>
            </div>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Domain (optional, leave blank for workgroup)
              </span>
              <input
                value={smbDomain}
                onChange={(e) => setSmbDomain(e.target.value)}
                placeholder="CORP"
                className="w-full input"
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={smbRecursive}
                onChange={(e) => setSmbRecursive(e.target.checked)}
              />
              Recurse into subfolders
            </label>
          </>
        ) : null}

        {kind === "gdrive" ? (
          <>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Folder ID
              </span>
              <input
                required
                value={gdriveFolderId}
                onChange={(e) => setGdriveFolderId(e.target.value)}
                placeholder="1AbC..._XyZ (from the Drive URL)"
                className="w-full input font-mono text-xs"
              />
              <span className="text-xs text-on-surface-variant">
                The part after <code>/folders/</code> in the Drive
                URL. Share the folder with the service
                account&apos;s email first.
              </span>
            </label>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Service-account JSON
              </span>
              <textarea
                required
                value={gdriveServiceJson}
                onChange={(e) => setGdriveServiceJson(e.target.value)}
                placeholder={
                  '{"type":"service_account","project_id":"...","private_key":"...",...}'
                }
                rows={6}
                className="w-full input font-mono text-xs"
              />
              <span className="text-xs text-on-surface-variant">
                Paste the raw JSON from Google Cloud Console →
                Service accounts → Keys → Add key. Stored encrypted
                at rest.
              </span>
            </label>
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={gdriveRecursive}
                onChange={(e) => setGdriveRecursive(e.target.checked)}
              />
              Recurse into subfolders
            </label>
          </>
        ) : null}

        {kind === "onedrive" ? (
          <>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Client ID (Azure AD app)
              </span>
              <input
                required
                value={oneDriveClientId}
                onChange={(e) => setOneDriveClientId(e.target.value)}
                placeholder="12345678-..."
                className="w-full input font-mono text-xs"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Tenant
              </span>
              <input
                value={oneDriveTenant}
                onChange={(e) => setOneDriveTenant(e.target.value)}
                placeholder="common"
                className="w-full input"
              />
              <span className="text-xs text-on-surface-variant">
                <code>common</code> for personal accounts;{" "}
                <code>organizations</code> for any business tenant;
                or a specific tenant ID.
              </span>
            </label>
            <div className="space-y-2 rounded-xl border border-outline/20 bg-surface-container-high/30 px-3 py-3">
              <div className="flex items-center gap-3 flex-wrap">
                <button
                  type="button"
                  onClick={() => void runOneDriveAuth()}
                  disabled={oneDriveAuthBusy || !oneDriveClientId}
                  className="rounded-xl bg-primary-container text-on-primary-container px-3 py-1.5 text-sm font-semibold disabled:opacity-50"
                >
                  {oneDriveAuthBusy ? "Authorising…" : "Authorise OneDrive"}
                </button>
                {oneDriveDeviceFlow ? (
                  <a
                    href={oneDriveDeviceFlow.verification_uri}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-primary underline text-sm"
                  >
                    Open {oneDriveDeviceFlow.verification_uri} ↗
                  </a>
                ) : null}
              </div>
              {oneDriveDeviceFlow ? (
                <div className="text-sm">
                  <span className="text-on-surface-variant">
                    Enter this code in the browser:{" "}
                  </span>
                  <span className="font-mono text-base font-semibold text-on-surface select-all">
                    {oneDriveDeviceFlow.user_code}
                  </span>
                </div>
              ) : null}
              {oneDriveAuthStatus ? (
                <div className="text-xs text-on-surface-variant">
                  {oneDriveAuthStatus}
                </div>
              ) : null}
            </div>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Access token (populated by Authorise button, or paste manually)
              </span>
              <textarea
                required
                value={oneDriveAccessToken}
                onChange={(e) => setOneDriveAccessToken(e.target.value)}
                placeholder="eyJ0eXAi..."
                rows={3}
                className="w-full input font-mono text-xs"
              />
              <span className="text-xs text-on-surface-variant">
                Use the Authorise button above — it runs the device-
                code flow against Microsoft and populates this field
                automatically. Refresh handled if refresh_token is
                also present.
              </span>
            </label>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Refresh token (optional)
              </span>
              <textarea
                value={oneDriveRefreshToken}
                onChange={(e) => setOneDriveRefreshToken(e.target.value)}
                rows={2}
                className="w-full input font-mono text-xs"
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Folder path
                </span>
                <input
                  value={oneDriveFolderPath}
                  onChange={(e) =>
                    setOneDriveFolderPath(e.target.value)
                  }
                  placeholder="/Documents/Policies"
                  className="w-full input"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Drive ID (optional)
                </span>
                <input
                  value={oneDriveDriveId}
                  onChange={(e) => setOneDriveDriveId(e.target.value)}
                  placeholder="b!ABCxyz..."
                  className="w-full input font-mono text-xs"
                />
              </label>
            </div>
          </>
        ) : null}

        {kind === "imap" ? (
          <>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1 col-span-2 sm:col-span-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  IMAP server
                </span>
                <input
                  required
                  value={imapServer}
                  onChange={(e) => setImapServer(e.target.value)}
                  placeholder="imap.gmail.com / outlook.office365.com"
                  className="w-full input"
                />
              </label>
              <label className="block space-y-1 col-span-2 sm:col-span-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Port
                </span>
                <input
                  type="number"
                  value={imapPort}
                  onChange={(e) => setImapPort(e.target.value)}
                  className="w-full input"
                />
              </label>
            </div>
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={imapSsl}
                onChange={(e) => setImapSsl(e.target.checked)}
              />
              Use SSL / TLS (recommended)
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Username
                </span>
                <input
                  required
                  value={imapUsername}
                  onChange={(e) => setImapUsername(e.target.value)}
                  placeholder="alice@example.com"
                  className="w-full input"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Password / app password
                </span>
                <input
                  required
                  type="password"
                  value={imapPassword}
                  onChange={(e) => setImapPassword(e.target.value)}
                  className="w-full input"
                />
              </label>
            </div>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Folder
              </span>
              <input
                value={imapFolder}
                onChange={(e) => setImapFolder(e.target.value)}
                placeholder="INBOX"
                className="w-full input"
              />
              <span className="text-xs text-on-surface-variant">
                Common: <code>INBOX</code>, <code>Sent</code>,{" "}
                <code>[Gmail]/All Mail</code>.
              </span>
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Crawl since (days)
                </span>
                <input
                  type="number"
                  min={1}
                  max={3650}
                  value={imapSinceDays}
                  onChange={(e) => setImapSinceDays(e.target.value)}
                  className="w-full input"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                  Max messages per crawl
                </span>
                <input
                  type="number"
                  min={1}
                  max={5000}
                  value={imapMaxMessages}
                  onChange={(e) => setImapMaxMessages(e.target.value)}
                  className="w-full input"
                />
              </label>
            </div>
            <label className="flex items-center gap-2 text-sm text-on-surface-variant">
              <input
                type="checkbox"
                checked={imapIncludeAttachments}
                onChange={(e) =>
                  setImapIncludeAttachments(e.target.checked)
                }
              />
              Include attachments (PDF / DOCX / images go through the
              same extractors)
            </label>
            <div className="rounded-xl border border-outline/20 bg-surface-container-high/30 px-3 py-2 text-xs text-on-surface-variant">
              <b>Gmail</b>: enable IMAP in settings, generate an{" "}
              <a
                href="https://myaccount.google.com/apppasswords"
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary underline"
              >
                app password
              </a>{" "}
              (2FA required), use that here — not your regular
              password.
              <br />
              <b>Outlook / Microsoft 365</b>: same pattern via{" "}
              <a
                href="https://account.microsoft.com/security"
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary underline"
              >
                app passwords
              </a>
              .
            </div>
          </>
        ) : null}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-xl bg-primary-container text-on-primary-container py-2 font-semibold disabled:opacity-50"
        >
          {busy ? "Creating…" : "Create source"}
        </button>
      </form>
    </GlassPanel>
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
