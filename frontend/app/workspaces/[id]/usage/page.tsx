"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import {
  getWorkspaceUsage,
  type UsageReport,
} from "@/lib/api";
import { cn } from "@/lib/cn";

export default function WorkspaceUsagePage() {
  const params = useParams<{ id: string }>();
  const workspaceId = params.id;
  const [report, setReport] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(30);

  useEffect(() => {
    setError(null);
    setReport(null);
    getWorkspaceUsage(workspaceId, days)
      .then(setReport)
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Failed to load usage"),
      );
  }, [workspaceId, days]);

  if (error) {
    return (
      <div className="p-6">
        <GlassPanel className="p-5 text-rose-400">{error}</GlassPanel>
      </div>
    );
  }
  if (!report) {
    return (
      <div className="p-6 text-on-surface-variant">Loading usage…</div>
    );
  }

  const t = report.totals;
  const maxLlm = Math.max(1, ...report.days.map((d) => d.llm_calls));

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="font-headline text-2xl text-on-surface">
          Workspace usage
        </h1>
        <select
          value={days}
          onChange={(e) => setDays(parseInt(e.target.value, 10))}
          className="bg-surface-variant/40 border border-outline/30 rounded px-2 py-1 text-sm"
        >
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="LLM calls" value={t.llm_calls} />
        <StatCard
          label="Tokens in / out"
          value={`${formatN(t.llm_tokens_in)} / ${formatN(t.llm_tokens_out)}`}
        />
        <StatCard
          label="Queries"
          value={`${t.queries_ok} ✓ / ${t.queries_failed} ✕`}
        />
        <StatCard
          label="RAG retrievals"
          value={t.rag_retrievals}
          sub={`${t.cache_hits} cache hits`}
        />
      </div>

      <GlassPanel className="p-5">
        <div className="font-headline text-on-surface mb-3">
          Daily LLM calls
        </div>
        {report.days.length === 0 ? (
          <div className="text-on-surface-variant text-sm">
            No activity recorded in this window.
          </div>
        ) : (
          <div className="flex items-end gap-1 h-32">
            {report.days.map((d) => (
              <div
                key={d.day}
                className="flex-1 flex flex-col items-center gap-1"
                title={`${d.day} — ${d.llm_calls} calls`}
              >
                <div
                  className={cn(
                    "w-full rounded-t",
                    d.llm_calls > 0
                      ? "bg-primary"
                      : "bg-surface-variant/30",
                  )}
                  style={{
                    height: `${Math.max(2, (d.llm_calls / maxLlm) * 100)}%`,
                  }}
                />
              </div>
            ))}
          </div>
        )}
      </GlassPanel>

      <GlassPanel className="p-5">
        <div className="font-headline text-on-surface mb-3">
          Per-day breakdown
        </div>
        <div className="overflow-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-on-surface-variant text-left">
                <th className="py-1.5 pr-3">Day</th>
                <th className="py-1.5 pr-3">LLM</th>
                <th className="py-1.5 pr-3">Tokens (in/out)</th>
                <th className="py-1.5 pr-3">Queries (ok/fail)</th>
                <th className="py-1.5 pr-3">RAG</th>
                <th className="py-1.5 pr-3">Cache</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {report.days.map((d) => (
                <tr
                  key={d.day}
                  className="border-t border-outline/20"
                >
                  <td className="py-1.5 pr-3">{d.day}</td>
                  <td className="py-1.5 pr-3">{d.llm_calls}</td>
                  <td className="py-1.5 pr-3">
                    {formatN(d.llm_tokens_in)} / {formatN(d.llm_tokens_out)}
                  </td>
                  <td className="py-1.5 pr-3">
                    {d.queries_ok} / {d.queries_failed}
                  </td>
                  <td className="py-1.5 pr-3">{d.rag_retrievals}</td>
                  <td className="py-1.5 pr-3">{d.cache_hits}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </GlassPanel>
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: number | string;
  sub?: string;
}) {
  return (
    <GlassPanel className="p-4">
      <div className="text-on-surface-variant text-xs uppercase tracking-wider">
        {label}
      </div>
      <div className="font-headline text-2xl text-on-surface mt-1">
        {typeof value === "number" ? formatN(value) : value}
      </div>
      {sub ? (
        <div className="text-on-surface-variant text-xs mt-0.5">
          {sub}
        </div>
      ) : null}
    </GlassPanel>
  );
}

function formatN(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
