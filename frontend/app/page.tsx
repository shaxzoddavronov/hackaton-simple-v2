"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { api, getToken } from "@/lib/api";

type WorkspaceOut = {
  id: string;
  name: string;
  status: string;
  connection_count: number;
};

const STATUS_TINT: Record<string, string> = {
  pending: "text-on-surface-variant",
  profiling: "text-secondary",
  ready: "text-tertiary",
  error: "text-error",
  auth_error: "text-error",
};

export default function WorkspacesPage() {
  const router = useRouter();
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
          <p className="font-mono text-label-caps uppercase text-on-surface-variant">
            Workspaces
          </p>
          <h1 className="font-headline text-headline-lg text-on-surface mt-1">
            Your connected databases
          </h1>
        </div>
        <Link
          href="/workspaces/new"
          className="rounded-xl bg-primary-container text-on-primary-container px-4 py-2 font-semibold hover:opacity-90"
        >
          + New workspace
        </Link>
      </div>

      {error ? (
        <GlassPanel className="px-5 py-4 text-error">{error}</GlassPanel>
      ) : items === null ? (
        <GlassPanel className="px-5 py-4 text-on-surface-variant">
          Loading…
        </GlassPanel>
      ) : items.length === 0 ? (
        <GlassPanel className="px-5 py-8 text-center">
          <p className="text-on-surface mb-2 font-headline text-xl">
            No workspaces yet.
          </p>
          <p className="text-on-surface-variant mb-4">
            Connect a Postgres or SQLite database to get started.
          </p>
          <Link
            href="/workspaces/new"
            className="inline-block rounded-xl bg-primary-container text-on-primary-container px-4 py-2 font-semibold"
          >
            Connect database
          </Link>
        </GlassPanel>
      ) : (
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {items.map((w) => (
            <GlassPanel key={w.id} className="px-5 py-4 space-y-2">
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-headline text-on-surface text-lg">
                    {w.name}
                  </div>
                  <div className="text-on-surface-variant text-sm">
                    {w.connection_count}{" "}
                    {w.connection_count === 1 ? "connection" : "connections"}
                  </div>
                </div>
                <span
                  className={
                    "text-xs uppercase tracking-wider " +
                    (STATUS_TINT[w.status] ?? "text-on-surface-variant")
                  }
                >
                  {w.status}
                </span>
              </div>
              <div className="flex gap-3 pt-2 text-sm">
                <Link
                  href={`/workspaces/${w.id}`}
                  className="text-primary hover:underline"
                >
                  Connections
                </Link>
                <Link
                  href={`/chat?workspace=${w.id}`}
                  className="text-primary hover:underline"
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
