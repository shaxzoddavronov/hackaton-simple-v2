"use client";

import { CodeBlock } from "@/components/CodeBlock";
import { RenderSpec } from "@/components/RenderSpec";
import { useToast } from "@/components/Toast";
import { createSavedQuestion } from "@/lib/api";
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
        <div className="max-w-[80%] rounded-2xl px-4 py-3 bg-primary-container/30 text-on-surface">
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
        <div className="text-on-surface-variant italic">No response.</div>
      )}
      {message.sql ? (
        <CodeBlock language="sql" code={message.sql} collapsible />
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
        "text-xs text-on-surface-variant hover:text-tertiary",
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
      <summary className="cursor-pointer text-on-surface-variant hover:text-on-surface flex items-center gap-2 select-none">
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
              className="rounded-xl border border-outline/20 bg-surface-container-high/30 px-3 py-2"
            >
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="text-xs font-mono text-primary">
                  [{i + 1}]
                </span>
                <span className="font-semibold text-on-surface text-sm">
                  {c.filename}
                </span>
                <span className="text-xs text-on-surface-variant uppercase tracking-wider">
                  {c.kind === "harvested_doc" ? "Harvested" : "Uploaded"}
                  {" · chunk "}
                  {c.chunk_index + 1}
                </span>
                {row && row.table ? (
                  <span className="text-xs font-mono text-tertiary">
                    ↳ {row.table}
                    {pkPairs ? ` (${pkPairs})` : ""}
                  </span>
                ) : null}
              </div>
              {c.snippet ? (
                <p className="mt-1 text-on-surface-variant text-xs leading-relaxed">
                  {c.snippet}
                </p>
              ) : null}
              {row && row.extras && Object.keys(row.extras).length > 0 ? (
                <div className="mt-1 flex flex-wrap gap-2 text-xs text-on-surface-variant">
                  {Object.entries(row.extras).map(([k, v]) => (
                    <span
                      key={k}
                      className="rounded-full bg-surface-container-high/50 border border-outline/20 px-2 py-0.5"
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
    <div className="flex flex-wrap gap-2 items-center text-xs text-on-surface-variant">
      <span className="uppercase tracking-wider">Federated · queried</span>
      {entries.map(([alias, summary]) => (
        <span
          key={alias}
          className="rounded-full bg-surface-container-high/50 border border-outline/20 px-2 py-0.5"
        >
          <span className="font-mono text-on-surface">{alias}</span>
          <span className="ml-1.5 opacity-70">
            {summary.row_count.toLocaleString()} rows · {summary.columns.length} cols
          </span>
        </span>
      ))}
    </div>
  );
}
