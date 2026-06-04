"use client";

import type { KPI } from "@/lib/types";

/** Coerce any JSON value to something React can render.
 *
 * The backend's KPI schema says ``value: float | str``, but a flaky
 * data path (Postgres JSONB cell, ES sub-aggregation, composite type
 * we forgot to flatten) can still hand us a dict or array. React
 * dies hard on those ("Objects are not valid as a React child"). We
 * stringify defensively so the UI shows *something* — the upstream
 * fix lives in ``chart_designer._coerce_to_primitive``. */
function renderable(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "number") return v.toLocaleString();
  if (typeof v === "string" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export function KPICard({ spec }: { spec: KPI }) {
  return (
    <div className="space-y-2">
      <div className="text-nd-fg-2 uppercase text-xs tracking-wider">
        {renderable(spec.label)}
      </div>
      <div className="flex items-baseline gap-2">
        <div className="font-mono text-3xl text-nd-fg-0">
          {renderable(spec.value)}
        </div>
        {spec.unit ? (
          <div className="text-nd-fg-2 text-sm">
            {renderable(spec.unit)}
          </div>
        ) : null}
      </div>
      {typeof spec.delta === "number" ? (
        <div
          className={
            spec.delta >= 0
              ? "text-tertiary text-sm"
              : "text-nd-error text-sm"
          }
        >
          {spec.delta >= 0 ? "▲" : "▼"} {Math.abs(spec.delta).toFixed(1)}%
        </div>
      ) : null}
    </div>
  );
}
