"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
import { api, getToken } from "@/lib/api";

type WorkspaceCreated = { id: string; name: string };

export default function NewWorkspacePage() {
  const router = useRouter();
  const toast = useToast();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  if (typeof window !== "undefined" && !getToken()) {
    router.replace("/login");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const ws = await api<WorkspaceCreated>("/workspaces", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      // Jump straight to the workspace detail page so the user can
      // add the first connection — the page transition is its own
      // success signal, no toast needed here.
      router.push(`/workspaces/${ws.id}`);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Failed to create workspace",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-2xl px-4 py-8 space-y-6">
      <header>
        <p className="font-mono text-label-caps uppercase text-nd-fg-2">
          New workspace
        </p>
        <h1 className="font-headline text-headline-lg text-nd-fg-0 mt-1">
          Create a workspace
        </h1>
        <p className="text-nd-fg-2 text-sm mt-1">
          A workspace is a folder. Inside it you connect one or more
          databases — Postgres, MySQL, ClickHouse, MongoDB, Elasticsearch,
          and more.
        </p>
      </header>

      <form onSubmit={submit} className="space-y-4">
        <GlassPanel className="px-5 py-4 space-y-3">
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wider text-nd-fg-2">
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

          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="w-full rounded-xl bg-nd-accent text-nd-on-accent py-2 font-semibold disabled:opacity-50"
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
