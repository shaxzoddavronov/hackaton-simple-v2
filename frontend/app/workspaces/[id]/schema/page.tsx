"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { api, getToken } from "@/lib/api";

type Item = {
  connection_id: string;
  name: string;
  dialect: string;
  status: string;
  bundle_ready: boolean;
  table_count: number | null;
};
type Resp = {
  workspace_id: string;
  name: string;
  connections: Item[];
};

export default function WorkspaceSchemaIndexPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const [data, setData] = useState<Resp | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api<Resp>(`/workspaces/${params.id}/schema`)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [params.id, router]);

  return (
    <main className="mx-auto max-w-3xl px-4 py-8 space-y-4">
      <header>
        <Link
          href={`/workspaces/${params.id}`}
          className="text-on-surface-variant text-sm hover:underline"
        >
          ← Connections
        </Link>
        <p className="font-mono text-label-caps uppercase text-on-surface-variant mt-2">
          Schema Explorer
        </p>
        <h1 className="font-headline text-headline-lg text-on-surface mt-1">
          {data?.name ?? "Loading…"}
        </h1>
        <p className="text-on-surface-variant text-sm mt-1">
          Pick a database to inspect its profiled schema.
        </p>
      </header>

      {error ? (
        <GlassPanel className="px-5 py-4 text-error">{error}</GlassPanel>
      ) : !data ? (
        <GlassPanel className="px-5 py-4 text-on-surface-variant">
          Loading…
        </GlassPanel>
      ) : data.connections.length === 0 ? (
        <GlassPanel className="px-5 py-4 text-on-surface-variant">
          No connections yet —{" "}
          <Link
            href={`/workspaces/${params.id}`}
            className="text-primary hover:underline"
          >
            add one
          </Link>
          .
        </GlassPanel>
      ) : (
        <div className="space-y-2">
          {data.connections.map((c) => (
            <GlassPanel key={c.connection_id} className="px-5 py-4">
              <div className="flex items-center justify-between">
                <div>
                  <div className="font-headline text-on-surface text-lg">
                    {c.name}
                  </div>
                  <div className="text-on-surface-variant text-sm uppercase tracking-wider">
                    {c.dialect}
                    {c.table_count != null
                      ? ` · ${c.table_count} tables`
                      : ""}
                  </div>
                </div>
                {c.bundle_ready ? (
                  <Link
                    href={`/workspaces/${params.id}/connections/${c.connection_id}/schema`}
                    className="text-primary hover:underline text-sm"
                  >
                    Open schema →
                  </Link>
                ) : (
                  <span className="text-on-surface-variant text-sm uppercase tracking-wider">
                    {c.status}
                  </span>
                )}
              </div>
            </GlassPanel>
          ))}
        </div>
      )}
    </main>
  );
}
