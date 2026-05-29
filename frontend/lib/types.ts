// Mirror of backend/app/schemas/ui_spec.py. The discriminator on `type`
// must match the backend Literal values exactly.

export type ColumnDef = {
  key: string;
  label: string;
  dtype?: "int" | "float" | "string" | "bool" | "datetime" | "date";
  align?: "left" | "right" | "center";
};

export type TextOnly = { type: "text_only"; body_md: string };

export type KPI = {
  type: "kpi";
  label: string;
  value: number | string;
  unit?: string | null;
  delta?: number | null;
  sparkline?: number[];
};

export type BarSpec = {
  type: "bar";
  title: string;
  x: string;
  y: string[];
  data: Record<string, unknown>[];
  stacked?: boolean;
};

export type LineSpec = {
  type: "line";
  title: string;
  x: string;
  y: string[];
  data: Record<string, unknown>[];
};

export type PieSpec = {
  type: "pie";
  title: string;
  label: string;
  value: string;
  data: Record<string, unknown>[];
};

export type TableSpec = {
  type: "table";
  columns: ColumnDef[];
  rows: unknown[][];
};

export type GridChild = { span: number; spec: UISpec };

export type Dashboard = {
  type: "dashboard";
  title: string;
  children: GridChild[];
};

export type UISpec =
  | TextOnly
  | KPI
  | BarSpec
  | LineSpec
  | PieSpec
  | TableSpec
  | Dashboard;

export type SubResultSummary = {
  columns: string[];
  row_count: number;
};

export type DbRowLink = {
  connection_id: string;
  table: string;
  // PK col → value (composite keys supported). May be empty when the
  // source table had no PK in the profiled schema bundle.
  row_pk: Record<string, string | number | boolean | null>;
  file_column: string;
  file_reference: string;
  extras?: Record<string, string | number | boolean | null>;
};

export type Citation = {
  kind: "user_doc" | "harvested_doc";
  source_id: string;
  filename: string;
  snippet: string;
  chunk_index: number;
  source_key: string;
  // Phase 17.1 — when the chunk came from a db_column source, this
  // field links back to the originating DB row so the UI can render
  // "policy.pdf from tickets where id=42".
  db_row?: DbRowLink;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  ui_spec?: UISpec | null;
  sql?: string | null;
  // Federated turns: per-sub-query breakdown keyed by the planner's
  // alias for that sub-result (e.g., "orders_pg", "events_es").
  sub_results?: Record<string, SubResultSummary> | null;
  // RAG citations: the chunks the retriever returned that the answer
  // was grounded in. Renders under the chart as a collapsible source
  // list with snippet previews.
  citations?: Citation[] | null;
};

export type WorkspaceOut = {
  id: string;
  name: string;
  status: string;
  connection_count: number;
};

export type ConnectionOut = {
  id: string;
  workspace_id: string;
  name: string;
  dialect:
    | "postgres"
    | "sqlite"
    | "mysql"
    | "clickhouse"
    | "oracle"
    | "mongodb"
    | "elasticsearch";
  status: string;
  profile_job_id?: string | null;
};
