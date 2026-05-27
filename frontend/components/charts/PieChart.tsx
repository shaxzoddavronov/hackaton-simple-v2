"use client";

import {
  Cell,
  Legend,
  Pie,
  PieChart as RPieChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

import type { PieSpec } from "@/lib/types";
import { sanitizeChartRows } from "./sanitize";

const PALETTE = ["#00d4ff", "#d2bbff", "#00ff88", "#a8e8ff", "#ffb4ab"];

export function PieChart({ spec }: { spec: PieSpec }) {
  const data = sanitizeChartRows(spec.data);
  return (
    <div className="w-full">
      <h3 className="font-headline text-on-surface text-lg mb-3">{spec.title}</h3>
      <ResponsiveContainer width="100%" height={260}>
        <RPieChart>
          <Pie
            data={data}
            dataKey={spec.value}
            nameKey={spec.label}
            outerRadius={100}
            label
          >
            {data.map((_, i) => (
              <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={{
              background: "#0f1f3a",
              border: "1px solid rgba(255,255,255,0.15)",
              borderRadius: 8,
              color: "#d7e2ff",
            }}
          />
          <Legend />
        </RPieChart>
      </ResponsiveContainer>
    </div>
  );
}
