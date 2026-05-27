// Shared sanitizer for chart-row data points.
//
// Recharts walks rows by `dataKey` and renders the result directly via
// React. If a cell happens to be an object like `{value: 0}` — which
// can leak in from an unflattened Elasticsearch sub-aggregation, a
// Postgres JSONB column, or a composite type — React throws
// "Objects are not valid as a React child" and the whole chat crashes.
//
// The upstream fix lives in the backend (chart_designer +
// engines/elasticsearch). This sanitizer is the last line of defence
// for charts loaded from already-persisted message ui_specs, where
// the bug has already happened and we can't re-run the agent.

export type ChartRow = Record<string, unknown>;

/** Unwrap a single cell to a primitive.
 *
 *  Rules:
 *    * ``null`` / ``undefined`` → unchanged so recharts treats them as
 *      missing.
 *    * Primitives (number / string / boolean) → unchanged.
 *    * ``{value}`` or ``{values}`` or ``{doc_count}`` → unwrapped.
 *      These are the shapes ES emits for unflattened metric aggs.
 *    * Any other object/array → JSON-encoded string. Loses chart
 *      utility (no longer numeric) but at least renders. */
function unwrapCell(v: unknown): unknown {
  if (v === null || v === undefined) return v;
  const t = typeof v;
  if (t === "number" || t === "string" || t === "boolean") return v;
  if (t === "object") {
    const obj = v as Record<string, unknown>;
    if ("value" in obj) return unwrapCell(obj.value);
    if ("values" in obj) return unwrapCell(obj.values);
    if ("doc_count" in obj) return unwrapCell(obj.doc_count);
    try {
      return JSON.stringify(obj);
    } catch {
      return String(obj);
    }
  }
  return v;
}

/** Sanitize each row's cells in place-free fashion. */
export function sanitizeChartRows(rows: ChartRow[]): ChartRow[] {
  return rows.map((row) => {
    const out: ChartRow = {};
    for (const k of Object.keys(row)) {
      out[k] = unwrapCell(row[k]);
    }
    return out;
  });
}
