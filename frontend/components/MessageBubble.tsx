"use client";

import { CodeBlock } from "@/components/CodeBlock";
import { RenderSpec } from "@/components/RenderSpec";
import { cn } from "@/lib/cn";
import type { ChatMessage } from "@/lib/types";

export function MessageBubble({ message }: { message: ChatMessage }) {
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
    </div>
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
