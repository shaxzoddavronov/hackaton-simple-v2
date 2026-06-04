"use client";

import { GlassPanel } from "@/components/GlassPanel";
import { KPICard } from "@/components/KPICard";
import { BarChart } from "@/components/charts/BarChart";
import { DataTable } from "@/components/charts/DataTable";
import { LineChart } from "@/components/charts/LineChart";
import { PieChart } from "@/components/charts/PieChart";
import type { UISpec } from "@/lib/types";

/**
 * Discriminated-union dispatcher for UISpec.
 *
 * The `type` tag must match the backend Pydantic models verbatim. Adding
 * a new variant requires updating: backend/app/schemas/ui_spec.py,
 * frontend/lib/types.ts, and this switch.
 */
export function RenderSpec({ spec }: { spec: UISpec }) {
  switch (spec.type) {
    case "text_only":
      return (
        <GlassPanel className="px-5 py-4 prose prose-invert max-w-none">
          <div className="whitespace-pre-wrap text-nd-fg-0">{spec.body_md}</div>
        </GlassPanel>
      );
    case "kpi":
      return (
        <GlassPanel className="px-5 py-4">
          <KPICard spec={spec} />
        </GlassPanel>
      );
    case "bar":
      return (
        <GlassPanel className="px-5 py-4">
          <BarChart spec={spec} />
        </GlassPanel>
      );
    case "line":
      return (
        <GlassPanel className="px-5 py-4">
          <LineChart spec={spec} />
        </GlassPanel>
      );
    case "pie":
      return (
        <GlassPanel className="px-5 py-4">
          <PieChart spec={spec} />
        </GlassPanel>
      );
    case "table":
      return (
        <GlassPanel className="px-5 py-4">
          <DataTable spec={spec} />
        </GlassPanel>
      );
    case "dashboard":
      return (
        <GlassPanel className="px-5 py-4">
          <h2 className="font-headline text-nd-fg-0 text-xl mb-4">
            {spec.title}
          </h2>
          <div className="grid grid-cols-12 gap-4">
            {spec.children.map((ch, i) => (
              <div key={i} className={`col-span-${Math.min(12, Math.max(1, ch.span))}`}>
                <RenderSpec spec={ch.spec} />
              </div>
            ))}
          </div>
        </GlassPanel>
      );
    default: {
      // Exhaustive — TS will flag a missing case
      const _exhaustive: never = spec;
      return null;
    }
  }
}
