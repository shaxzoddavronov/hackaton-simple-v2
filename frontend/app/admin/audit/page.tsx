"use client";

/**
 * Phase 16 — audit log timeline.
 *
 * Lists the 100 most-recent audit rows, filterable by action
 * prefix (e.g. ``user.``, ``auth.``, ``http.GET``) and outcome
 * (ok / denied / error). The middleware writes coarse
 * `http.{METHOD}.{path_pattern}` rows for every non-noisy
 * request; explicit handler-side calls add finer-grained
 * `user.create`, `auth.login`, etc.
 */
import { useCallback, useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { listAudit, type AuditEntry } from "@/lib/api";
import { cn } from "@/lib/cn";


export default function AdminAuditPage() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [action, setAction] = useState("");
  const [statusFilter, setStatusFilter] = useState<
    "" | "ok" | "denied" | "error"
  >("");

  const reload = useCallback(async () => {
    setError(null);
    try {
      const rows = await listAudit({
        action: action.trim() || undefined,
        status: statusFilter || undefined,
        limit: 200,
      });
      setEntries(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load audit");
    }
  }, [action, statusFilter]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return (
    <div className="p-6 space-y-4 max-w-6xl">
      <div className="flex items-baseline justify-between">
        <h1 className="font-headline text-2xl text-on-surface">
          Audit log
        </h1>
        <button
          type="button"
          onClick={reload}
          className="text-xs px-3 py-1.5 rounded-xl bg-surface-variant/40 hover:bg-surface-variant/70"
        >
          ↻ Refresh
        </button>
      </div>

      <GlassPanel className="p-4">
        <div className="flex flex-wrap gap-3 items-end">
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wider text-on-surface-variant">
              Action prefix
            </span>
            <input
              value={action}
              onChange={(e) => setAction(e.target.value)}
              placeholder="user. · auth. · http.POST · …"
              className="input"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wider text-on-surface-variant">
              Status
            </span>
            <select
              value={statusFilter}
              onChange={(e) =>
                setStatusFilter(
                  e.target.value as "" | "ok" | "denied" | "error",
                )
              }
              className="input"
            >
              <option value="">Any</option>
              <option value="ok">ok</option>
              <option value="denied">denied</option>
              <option value="error">error</option>
            </select>
          </label>
        </div>
      </GlassPanel>

      {error ? (
        <GlassPanel className="p-5 text-rose-400">{error}</GlassPanel>
      ) : !entries ? (
        <div className="text-on-surface-variant">Loading audit…</div>
      ) : entries.length === 0 ? (
        <GlassPanel className="p-5 text-on-surface-variant">
          No entries match the current filters.
        </GlassPanel>
      ) : (
        <GlassPanel className="overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-surface-variant/30">
              <tr className="text-left text-on-surface-variant text-xs uppercase tracking-wider">
                <th className="p-3">When</th>
                <th className="p-3">Action</th>
                <th className="p-3">Status</th>
                <th className="p-3">Target</th>
                <th className="p-3">User</th>
                <th className="p-3">IP</th>
                <th className="p-3">Payload</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr
                  key={e.id}
                  className="border-t border-outline/15 align-top"
                >
                  <td className="p-3 text-xs text-on-surface-variant font-mono whitespace-nowrap">
                    {new Date(e.created_at).toLocaleString()}
                  </td>
                  <td className="p-3 font-mono text-xs">{e.action}</td>
                  <td className="p-3">
                    <span
                      className={cn(
                        "px-2 py-0.5 text-xs uppercase tracking-wider rounded",
                        e.status === "ok" &&
                          "bg-emerald-500/15 text-emerald-300",
                        e.status === "denied" &&
                          "bg-amber-500/15 text-amber-300",
                        e.status === "error" &&
                          "bg-rose-500/15 text-rose-300",
                      )}
                    >
                      {e.status}
                    </span>
                  </td>
                  <td className="p-3 text-xs font-mono">
                    {e.target_kind ? (
                      <>
                        <span className="text-on-surface-variant">
                          {e.target_kind}:
                        </span>{" "}
                        {e.target_id}
                      </>
                    ) : (
                      <span className="text-on-surface-variant">—</span>
                    )}
                  </td>
                  <td className="p-3 text-xs font-mono">
                    {e.user_id ? (
                      e.user_id.slice(0, 8)
                    ) : (
                      <span className="text-on-surface-variant">anon</span>
                    )}
                  </td>
                  <td className="p-3 text-xs font-mono text-on-surface-variant">
                    {e.client_ip ?? "—"}
                  </td>
                  <td className="p-3 text-xs font-mono text-on-surface-variant max-w-md">
                    {Object.keys(e.payload || {}).length > 0 ? (
                      <pre className="whitespace-pre-wrap break-words">
                        {JSON.stringify(e.payload, null, 0)}
                      </pre>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </GlassPanel>
      )}
    </div>
  );
}
