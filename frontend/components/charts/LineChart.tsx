"use client";

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart as RLineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { LineSpec } from "@/lib/types";
import { sanitizeChartRows } from "./sanitize";

const PALETTE = ["#00d4ff", "#d2bbff", "#00ff88", "#a8e8ff"];

export function LineChart({ spec }: { spec: LineSpec }) {
  const data = sanitizeChartRows(spec.data);
  return (
    <div className="w-full">
      <h3 className="font-headline text-on-surface text-lg mb-3">{spec.title}</h3>
      <ResponsiveContainer width="100%" height={260}>
        <RLineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.08)" />
          <XAxis dataKey={spec.x} stroke="#bbc9cf" />
          <YAxis stroke="#bbc9cf" />
          <Tooltip
            contentStyle={{
              background: "#0f1f3a",
              border: "1px solid rgba(255,255,255,0.15)",
              borderRadius: 8,
              color: "#d7e2ff",
            }}
          />
          <Legend />
          {spec.y.map((k, i) => (
            <Line
              key={k}
              type="monotone"
              dataKey={k}
              stroke={PALETTE[i % PALETTE.length]}
              strokeWidth={2}
              dot={{ r: 3 }}
            />
          ))}
        </RLineChart>
      </ResponsiveContainer>
    </div>
  );
}
