"use client";

import { useCallback, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface CodeBlockProps {
  code: string;
  language: string;
  /**
   * When true, wraps the block in a `<details>` element so chat history can
   * fold long SQL by default. The `<summary>` shows the language label.
   */
  collapsible?: boolean;
  className?: string;
}

/**
 * `<CodeBlock>` — monospace, syntax-highlight-friendly code rendering.
 * Used directly under every assistant message in chat to show the SQL
 * the agent produced (see CLAUDE.md §Design system). Highlighting itself
 * is left to a syntax library hook (Shiki / Prism) wired up later — for
 * now we expose the `language-${lang}` class hook on the `<code>` element.
 */
export function CodeBlock({
  code,
  language,
  collapsible = false,
  className,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    if (typeof navigator === "undefined" || !navigator.clipboard) return;
    navigator.clipboard.writeText(code).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => {
        // clipboard write failed (e.g. permissions) — leave button silent
      },
    );
  }, [code]);

  const body = (
    <div
      className={cn(
        "relative overflow-hidden rounded-lg border border-nd-border-subtle bg-nd-bg-1",
        className,
      )}
    >
      <button
        type="button"
        onClick={handleCopy}
        aria-label={copied ? "Copied" : "Copy code"}
        className={cn(
          "absolute right-stack-md top-stack-md z-10",
          "rounded-md border border-nd-border-subtle bg-nd-bg-1",
          "px-stack-md py-stack-sm",
          "font-mono text-label-caps uppercase",
          "text-nd-fg-2 transition-colors",
          "hover:border-nd-accent hover:text-nd-accent",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-nd-accent",
        )}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <pre
        className={cn(
          "overflow-x-auto px-container-margin py-stack-lg pr-20",
          "font-mono text-data-mono text-nd-fg-0",
        )}
      >
        <code className={`language-${language}`}>{code}</code>
      </pre>
    </div>
  );

  if (!collapsible) return body;

  return (
    <details
      className={cn(
        "group rounded-lg border border-nd-border-subtle bg-nd-bg-1",
        className,
      )}
    >
      <summary
        className={cn(
          "flex cursor-pointer select-none items-center justify-between",
          "px-container-margin py-stack-md",
          "font-mono text-label-caps uppercase text-nd-fg-2",
          "hover:text-nd-accent",
          "marker:hidden [&::-webkit-details-marker]:hidden",
        )}
      >
        <span>{language}</span>
        <Chevron />
      </summary>
      <div className="px-stack-md pb-stack-md">{body}</div>
    </details>
  );
}

function Chevron(): ReactNode {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden
      className="h-3 w-3 transition-transform group-open:rotate-180"
    >
      <path
        fillRule="evenodd"
        d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.06l3.71-3.83a.75.75 0 1 1 1.08 1.04l-4.25 4.39a.75.75 0 0 1-1.08 0L5.21 8.27a.75.75 0 0 1 .02-1.06Z"
        clipRule="evenodd"
      />
    </svg>
  );
}

export default CodeBlock;
