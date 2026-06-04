"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { useT } from "@/lib/i18n/context";
import { api, getToken } from "@/lib/api";

type WorkspaceOut = {
  id: string;
  name: string;
  status: string;
  connection_count: number;
};

/** Phase 43 — Neural Dark v2 status palette. Maps the canonical
 *  workspace/connection statuses to semantic CSS vars. */
const STATUS_COLOR: Record<string, string> = {
  pending: "var(--status-neutral)",
  profiling: "var(--status-activity)",
  ready: "var(--status-ready)",
  error: "var(--status-error)",
  auth_error: "var(--status-auth)",
};

export default function WorkspacesPage() {
  const router = useRouter();
  const t = useT();
  const [items, setItems] = useState<WorkspaceOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api<WorkspaceOut[]>("/workspaces")
      .then(setItems)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [router]);

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <div className="flex items-end justify-between mb-6">
        <div>
          <p className="qm-overline">{t.ws_title}</p>
          <h1 className="qm-h1 mt-1">{t.ws_subtitle}</h1>
        </div>
        <Link
          href="/workspaces/new"
          className="rounded-[10px] px-4 py-2 font-semibold transition-colors"
          style={{
            backgroundColor: "var(--accent)",
            color: "var(--on-accent)",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.backgroundColor = "var(--accent-hover)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.backgroundColor = "var(--accent)";
          }}
        >
          {t.ws_new}
        </Link>
      </div>

      {error ? (
        <GlassPanel
          className="px-5 py-4"
          style={{ color: "var(--status-error)" }}
        >
          {error}
        </GlassPanel>
      ) : items === null ? (
        <GlassPanel
          className="px-5 py-4"
          style={{ color: "var(--fg-2)" }}
        >
          {t.btn_loading}
        </GlassPanel>
      ) : items.length === 0 ? (
        <GlassPanel className="px-5 py-10 text-center space-y-3">
          <h2 className="qm-h2">{t.ws_empty}</h2>
          <Link
            href="/workspaces/new"
            className="inline-block rounded-[10px] px-4 py-2 font-semibold"
            style={{
              backgroundColor: "var(--accent)",
              color: "var(--on-accent)",
            }}
          >
            {t.ws_new}
          </Link>
        </GlassPanel>
      ) : (
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {items.map((w) => (
            <GlassPanel
              key={w.id}
              className="px-5 py-4 space-y-3 transition-colors"
              style={{
                cursor: "default",
              }}
              onMouseEnter={(e: React.MouseEvent<HTMLDivElement>) => {
                e.currentTarget.style.backgroundColor =
                  "var(--bg-hover)";
              }}
              onMouseLeave={(e: React.MouseEvent<HTMLDivElement>) => {
                e.currentTarget.style.backgroundColor = "var(--bg-2)";
              }}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div
                    className="font-headline text-lg truncate"
                    style={{ color: "var(--fg-0)" }}
                  >
                    {w.name}
                  </div>
                  <div
                    className="qm-body-sm"
                    style={{ color: "var(--fg-2)" }}
                  >
                    {t.ws_connections_count(w.connection_count)}
                  </div>
                </div>
                <span
                  className="qm-overline shrink-0 inline-flex items-center gap-1.5"
                  style={{
                    color:
                      STATUS_COLOR[w.status] ?? "var(--fg-2)",
                  }}
                >
                  <span
                    aria-hidden
                    className="inline-block w-1.5 h-1.5 rounded-full"
                    style={{
                      backgroundColor:
                        STATUS_COLOR[w.status] ?? "var(--fg-3)",
                    }}
                  />
                  {w.status}
                </span>
              </div>
              <div className="flex gap-4 pt-2 text-sm">
                <Link
                  href={`/workspaces/${w.id}`}
                  className="hover:underline"
                  style={{ color: "var(--accent)" }}
                >
                  Connections
                </Link>
                <Link
                  href={`/chat?workspace=${w.id}`}
                  className="hover:underline"
                  style={{ color: "var(--accent)" }}
                >
                  Open chat
                </Link>
              </div>
            </GlassPanel>
          ))}
        </div>
      )}
    </main>
  );
}
