"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { api, getToken } from "@/lib/api";

// Shape mirrors backend `app/schemas/schema_bundle.py`.
type Sample = {
  distinct_values?: unknown[] | null;
  distinct_truncated?: boolean;
  numeric_stats?: Record<string, number> | null;
  sample_rows?: unknown[] | null;
  non_null_count?: number | null;
};
type Column = {
  name: string;
  data_type: string;
  nullable: boolean;
  is_pk?: boolean;
  is_id?: boolean;
  fk_to?: string | null;
};
type Table = {
  schema: string;
  name: string;
  columns: Column[];
  foreign_keys: { from_columns: string[]; to_table: string; to_columns: string[] }[];
  row_count_estimate?: number | null;
};
type Bundle = {
  dialect: string;
  tables: Table[];
  samples: Record<string, Record<string, Sample>>;
};
type Resp = {
  workspace_id: string;
  connection_id: string;
  status: string;
  bundle: Bundle | null;
  message?: string;
  refreshed_at?: string;
};

export default function ConnectionSchemaPage() {
  const router = useRouter();
  const params = useParams<{ id: string; cid: string }>();
  const [data, setData] = useState<Resp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api<Resp>(
      `/workspaces/${params.id}/connections/${params.cid}/schema`,
    )
      .then((d) => {
        setData(d);
        const first = d.bundle?.tables?.[0];
        if (first) {
          setSelected(`${first.schema}.${first.name}`);
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [params.id, params.cid, router]);

  if (error)
    return (
      <PageShell workspaceId={params.id}>
        <GlassPanel className="px-5 py-4 text-nd-error">{error}</GlassPanel>
      </PageShell>
    );
  if (!data)
    return (
      <PageShell workspaceId={params.id}>
        <GlassPanel className="px-5 py-4 text-nd-fg-2">
          Loading…
        </GlassPanel>
      </PageShell>
    );
  if (!data.bundle) {
    return (
      <PageShell workspaceId={params.id}>
        <GlassPanel className="px-5 py-4">
          <div className="text-nd-fg-0">Status: {data.status}</div>
          <div className="text-nd-fg-2 text-sm mt-1">
            {data.message ?? "Bundle not available."}
          </div>
        </GlassPanel>
      </PageShell>
    );
  }

  const selectedTable = data.bundle.tables.find(
    (t) => `${t.schema}.${t.name}` === selected,
  );
  const samples = (selected && data.bundle.samples[selected]) || {};

  return (
    <PageShell workspaceId={params.id} dialect={data.bundle.dialect}>
      <div className="grid grid-cols-12 gap-4">
        <GlassPanel className="col-span-4 px-3 py-3 overflow-y-auto max-h-[80vh]">
          <div className="text-xs uppercase tracking-wider text-nd-fg-2 px-2 pb-2">
            Tables ({data.bundle.tables.length})
          </div>
          <ul className="space-y-0.5">
            {data.bundle.tables.map((t) => {
              const qn = `${t.schema}.${t.name}`;
              const active = qn === selected;
              return (
                <li key={qn}>
                  <button
                    onClick={() => setSelected(qn)}
                    className={
                      "w-full text-left px-3 py-2 rounded-lg font-mono text-sm " +
                      (active
                        ? "bg-nd-accent-wash text-nd-accent"
                        : "text-nd-fg-0 hover:bg-nd-bg-1")
                    }
                  >
                    {t.name}
                    <span className="ml-2 text-xs text-nd-fg-2">
                      {t.columns.length} cols
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </GlassPanel>

        <div className="col-span-8 space-y-4">
          {selectedTable ? (
            <>
              <GlassPanel className="px-5 py-4">
                <div className="flex items-baseline justify-between">
                  <h2 className="font-headline text-nd-fg-0 text-xl">
                    {selectedTable.schema}.{selectedTable.name}
                  </h2>
                  {selectedTable.row_count_estimate != null ? (
                    <span className="text-nd-fg-2 text-sm">
                      ~{selectedTable.row_count_estimate.toLocaleString()} rows
                    </span>
                  ) : null}
                </div>
                <table className="w-full mt-3 text-sm">
                  <thead>
                    <tr className="text-left border-b border-nd-border-subtle text-nd-fg-2 uppercase text-xs tracking-wider">
                      <th className="py-2">Column</th>
                      <th>Type</th>
                      <th>Null?</th>
                      <th>Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedTable.columns.map((c) => (
                      <tr key={c.name} className="border-b border-nd-border-subtle">
                        <td className="py-2 font-mono">{c.name}</td>
                        <td className="text-nd-fg-2">{c.data_type}</td>
                        <td className="text-nd-fg-2">
                          {c.nullable ? "yes" : "no"}
                        </td>
                        <td className="text-nd-fg-2 space-x-2">
                          {c.is_pk ? <span className="text-nd-accent">PK</span> : null}
                          {c.is_id ? <span className="text-secondary">ID</span> : null}
                          {c.fk_to ? (
                            <span className="text-tertiary">→ {c.fk_to}</span>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </GlassPanel>

              <GlassPanel className="px-5 py-4">
                <div className="text-xs uppercase tracking-wider text-nd-fg-2 pb-2">
                  Samples
                </div>
                <div className="space-y-3">
                  {Object.entries(samples).map(([col, s]) => (
                    <div key={col} className="text-sm">
                      <div className="font-mono text-nd-fg-0">{col}</div>
                      <div className="text-nd-fg-2 ml-3">
                        {s.distinct_values ? (
                          <>
                            distinct:{" "}
                            <span className="font-mono">
                              {s.distinct_values
                                .map((v) => JSON.stringify(v))
                                .join(", ")}
                              {s.distinct_truncated ? ", …" : ""}
                            </span>
                          </>
                        ) : s.numeric_stats ? (
                          <span className="font-mono">
                            {Object.entries(s.numeric_stats)
                              .map(([k, v]) => `${k}=${v}`)
                              .join(", ")}
                          </span>
                        ) : s.sample_rows ? (
                          <span className="font-mono">
                            sample:{" "}
                            {s.sample_rows.map((v) => JSON.stringify(v)).join(", ")}
                          </span>
                        ) : (
                          <span className="opacity-60">no sample</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </GlassPanel>
            </>
          ) : null}
        </div>
      </div>
    </PageShell>
  );
}

function PageShell({
  workspaceId,
  dialect,
  children,
}: {
  workspaceId: string;
  dialect?: string;
  children: React.ReactNode;
}) {
  return (
    <main className="mx-auto max-w-6xl px-4 py-8 space-y-4">
      <header>
        <Link
          href={`/workspaces/${workspaceId}`}
          className="text-nd-fg-2 text-sm hover:underline"
        >
          ← Connections
        </Link>
        <p className="font-mono text-label-caps uppercase text-nd-fg-2 mt-2">
          Schema Explorer
        </p>
        <h1 className="font-headline text-headline-lg text-nd-fg-0 mt-1">
          Profiled schema{dialect ? ` · ${dialect}` : ""}
        </h1>
      </header>
      {children}
    </main>
  );
}
