"use client";

import type { TableSpec } from "@/lib/types";
import { cn } from "@/lib/cn";

export function DataTable({ spec }: { spec: TableSpec }) {
  return (
    <div className="w-full overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b border-nd-border-subtle">
            {spec.columns.map((c) => (
              <th
                key={c.key}
                className={cn(
                  "px-3 py-2 font-headline text-nd-fg-2 uppercase text-xs tracking-wider",
                  c.align === "right" && "text-right",
                  c.align === "center" && "text-center",
                  (!c.align || c.align === "left") && "text-left",
                )}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {spec.rows.map((row, i) => (
            <tr
              key={i}
              className="border-b border-nd-border-subtle hover:bg-nd-bg-1"
            >
              {row.map((cell, j) => {
                const colDef = spec.columns[j];
                const cls = cn(
                  "px-3 py-2 text-nd-fg-0",
                  colDef?.align === "right" && "text-right",
                  colDef?.align === "center" && "text-center",
                );
                return (
                  <td key={j} className={cls}>
                    {String(cell ?? "")}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
