"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { api, getToken } from "@/lib/api";

type WorkspaceCreated = { id: string; name: string };

export default function NewWorkspacePage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (typeof window !== "undefined" && !getToken()) {
    router.replace("/login");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const ws = await api<WorkspaceCreated>("/workspaces", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      // Jump straight to the workspace detail page so the user can
      // add the first connection.
      router.push(`/workspaces/${ws.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workspace");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-2xl px-4 py-8 space-y-6">
      <header>
        <p className="font-mono text-label-caps uppercase text-on-surface-variant">
          New workspace
        </p>
        <h1 className="font-headline text-headline-lg text-on-surface mt-1">
          Create a workspace
        </h1>
        <p className="text-on-surface-variant text-sm mt-1">
          A workspace is a folder. Inside it you connect one or more
          databases — Postgres, MySQL, ClickHouse, MongoDB, Elasticsearch,
          and more.
        </p>
      </header>

      <form onSubmit={submit} className="space-y-4">
        <GlassPanel className="px-5 py-4 space-y-3">
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wider text-on-surface-variant">
              Name
            </span>
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Core_Analytics"
              className="w-full input"
            />
          </label>

          {error ? <div className="text-error text-sm">{error}</div> : null}
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="w-full rounded-xl bg-primary-container text-on-primary-container py-2 font-semibold disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create workspace"}
          </button>
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
