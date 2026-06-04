"use client";

import { useState } from "react";

import { CodeBlock } from "@/components/CodeBlock";
import { RenderSpec } from "@/components/RenderSpec";
import { useToast } from "@/components/Toast";
import {
  createSavedQuestion,
  downloadMessageExport,
  type ExportFormat,
} from "@/lib/api";
import { cn } from "@/lib/cn";
import type { ChatMessage } from "@/lib/types";

/**
 * Renders one chat message.
 *
 * Optional ``previousUserPrompt`` / ``workspaceId`` / ``connectionId``
 * are passed through from the chat page so the Star button can save
 * the upstream user message that produced this assistant answer.
 */
export function MessageBubble({
  message,
  previousUserPrompt,
  workspaceId,
  connectionId,
}: {
  message: ChatMessage;
  previousUserPrompt?: string;
  workspaceId?: string | null;
  connectionId?: string | null;
}) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl px-4 py-3 bg-nd-accent-wash text-nd-fg-0">
          {message.content}
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {message.sub_results && Object.keys(message.sub_results).length ? (
        <FederationBadge subResults={message.sub_results} />
      ) : null}
      {message.ui_spec ? (
        <RenderSpec spec={message.ui_spec} />
      ) : (
        <div className="text-nd-fg-2 italic">No response.</div>
      )}
      {message.sql ? (
        <div className="flex items-start gap-2">
          <div className="flex-1 min-w-0">
            <CodeBlock language="sql" code={message.sql} collapsible />
          </div>
          <ExportMenu messageId={message.id} />
        </div>
      ) : null}
      {message.citations && message.citations.length ? (
        <CitationsList citations={message.citations} />
      ) : null}
      {previousUserPrompt && workspaceId ? (
        <StarButton
          prompt={previousUserPrompt}
          workspaceId={workspaceId}
          connectionId={connectionId ?? null}
        />
      ) : null}
    </div>
  );
}

function StarButton({
  prompt,
  workspaceId,
  connectionId,
}: {
  prompt: string;
  workspaceId: string;
  connectionId: string | null;
}) {
  const toast = useToast();
  async function onStar() {
    const title = window.prompt(
      "Save question as:",
      prompt.slice(0, 60),
    );
    if (!title || !title.trim()) return;
    try {
      await createSavedQuestion(workspaceId, {
        title: title.trim(),
        prompt,
        connection_id: connectionId,
      });
      toast.success(
        "Starred — find it in Dashboards → Inbox",
      );
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to star question",
      );
    }
  }
  return (
    <button
      type="button"
      onClick={() => void onStar()}
      title="Save this question for re-running from a dashboard"
      className={cn(
        "text-xs text-nd-fg-2 hover:text-nd-accent",
        "flex items-center gap-1",
      )}
    >
      <span>⭐</span>
      <span>Star question</span>
    </button>
  );
}

function CitationsList({
  citations,
}: {
  citations: NonNullable<ChatMessage["citations"]>;
}) {
  return (
    <details className="text-sm group">
      <summary className="cursor-pointer text-nd-fg-2 hover:text-nd-fg-0 flex items-center gap-2 select-none">
        <span className="uppercase tracking-wider text-xs">
          Sources · {citations.length}
        </span>
        <span className="text-xs opacity-60">
          (click to expand snippets)
        </span>
      </summary>
      <ol className="mt-2 space-y-2">
        {citations.map((c, i) => {
          const row = c.db_row;
          const pkPairs = row
            ? Object.entries(row.row_pk || {})
                .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                .join(", ")
            : "";
          return (
            <li
              key={c.source_key}
              className="rounded-xl border border-nd-border-subtle bg-nd-bg-1 px-3 py-2"
            >
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="text-xs font-mono text-nd-accent">
                  [{i + 1}]
                </span>
                <span className="font-semibold text-nd-fg-0 text-sm">
                  {c.filename}
                </span>
                <span className="text-xs text-nd-fg-2 uppercase tracking-wider">
                  {c.kind === "harvested_doc" ? "Harvested" : "Uploaded"}
                  {" · chunk "}
                  {c.chunk_index + 1}
                </span>
                {row && row.table ? (
                  <span className="text-xs font-mono text-nd-ready">
                    ↳ {row.table}
                    {pkPairs ? ` (${pkPairs})` : ""}
                  </span>
                ) : null}
              </div>
              {c.snippet ? (
                <p className="mt-1 text-nd-fg-2 text-xs leading-relaxed">
                  {c.snippet}
                </p>
              ) : null}
              {row && row.extras && Object.keys(row.extras).length > 0 ? (
                <div className="mt-1 flex flex-wrap gap-2 text-xs text-nd-fg-2">
                  {Object.entries(row.extras).map(([k, v]) => (
                    <span
                      key={k}
                      className="rounded-full bg-nd-bg-1 border border-nd-border-subtle px-2 py-0.5"
                    >
                      <span className="font-mono">{k}:</span>{" "}
                      <span>{String(v)}</span>
                    </span>
                  ))}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
    </details>
  );
}

function FederationBadge({
  subResults,
}: {
  subResults: NonNullable<ChatMessage["sub_results"]>;
}) {
  const entries = Object.entries(subResults);
  return (
    <div className="flex flex-wrap gap-2 items-center text-xs text-nd-fg-2">
      <span className="uppercase tracking-wider">Federated · queried</span>
      {entries.map(([alias, summary]) => (
        <span
          key={alias}
          className="rounded-full bg-nd-bg-1 border border-nd-border-subtle px-2 py-0.5"
        >
          <span className="font-mono text-nd-fg-0">{alias}</span>
          <span className="ml-1.5 opacity-70">
            {summary.row_count.toLocaleString()} rows · {summary.columns.length} cols
          </span>
        </span>
      ))}
    </div>
  );
}


/**
 * Phase 34 — download menu next to the SQL block. Three formats:
 * CSV (Excel-friendly UTF-8 BOM), XLSX (styled, autofit columns),
 * JSON (one object per row).
 *
 * Cache miss (oversize result, pre-Phase-34 message) surfaces as a
 * toast — the backend returns 410 with a re-run hint.
 */
function ExportMenu({ messageId }: { messageId: string }) {
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [open, setOpen] = useState(false);
  const toast = useToast();

  async function download(fmt: ExportFormat) {
    setBusy(fmt);
    setOpen(false);
    try {
      await downloadMessageExport(messageId, fmt);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Download failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={busy !== null}
        className={cn(
          "px-2.5 py-1 rounded-md text-xs",
          "bg-nd-bg-1 hover:bg-nd-bg-hover",
          "text-nd-fg-0 border border-nd-border",
          "transition disabled:opacity-50",
        )}
        title="Download result rows"
      >
        {busy ? `…${busy}` : "↓ Export"}
      </button>
      {open ? (
        <div
          className={cn(
            "absolute right-0 mt-1 z-20 w-32",
            "rounded-md border border-nd-border bg-nd-bg-2",
            "shadow-lg overflow-hidden",
          )}
          onMouseLeave={() => setOpen(false)}
        >
          {(["csv", "xlsx", "json"] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => download(f)}
              className={cn(
                "block w-full text-left px-3 py-1.5 text-xs",
                "hover:bg-nd-bg-hover text-nd-fg-0",
              )}
            >
              {f === "csv"
                ? "CSV"
                : f === "xlsx"
                  ? "Excel (.xlsx)"
                  : "JSON"}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
