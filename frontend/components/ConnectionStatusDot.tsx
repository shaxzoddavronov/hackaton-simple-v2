"use client";

import { useState } from "react";

import { getConnectionHealth, type ConnectionHealth } from "@/lib/api";
import { cn } from "@/lib/cn";

/**
 * Phase 35 — green / red / grey status dot next to a connection
 * name in the workspace sidebar.
 *
 * Source of truth: the four `last_health_*` columns on the
 * connection row. The Celery beat fills these every 5 minutes; the
 * "↻" button next to the dot forces an on-demand probe via
 * GET /workspaces/{wid}/connections/{cid}/health?refresh=true.
 */
type Props = {
  workspaceId: string;
  connectionId: string;
  initialOk: boolean | null | undefined;
  initialLatencyMs?: number | null;
  initialError?: string | null;
  initialCheckedAt?: string | null;
};

export function ConnectionStatusDot({
  workspaceId,
  connectionId,
  initialOk,
  initialLatencyMs,
  initialError,
  initialCheckedAt,
}: Props) {
  const [health, setHealth] = useState<ConnectionHealth>({
    connection_id: connectionId,
    dialect: "" as ConnectionHealth["dialect"],
    last_health_check_at: initialCheckedAt ?? null,
    last_health_ok: initialOk ?? null,
    last_health_latency_ms: initialLatencyMs ?? null,
    last_health_error: initialError ?? null,
  });
  const [busy, setBusy] = useState(false);

  async function recheck() {
    setBusy(true);
    try {
      const next = await getConnectionHealth(
        workspaceId,
        connectionId,
        true,
      );
      setHealth(next);
    } catch {
      // Network failure on the probe itself — leave the previous
      // state alone so the dot doesn't lie about being healthy.
    } finally {
      setBusy(false);
    }
  }

  const tone =
    health.last_health_ok === true
      ? "ok"
      : health.last_health_ok === false
        ? "fail"
        : "unknown";

  const dotClasses = cn(
    "inline-block w-2.5 h-2.5 rounded-full shrink-0",
    tone === "ok" && "bg-emerald-400 shadow-[0_0_4px_rgb(52_211_153)]",
    tone === "fail" && "bg-rose-500 shadow-[0_0_4px_rgb(244_63_94)]",
    tone === "unknown" && "bg-on-surface-variant/40",
  );

  const tooltip = (() => {
    if (tone === "unknown") return "Never checked";
    const at = health.last_health_check_at
      ? new Date(health.last_health_check_at).toLocaleString()
      : "unknown time";
    if (tone === "ok") {
      const lat =
        health.last_health_latency_ms != null
          ? ` · ${health.last_health_latency_ms} ms`
          : "";
      return `Healthy · ${at}${lat}`;
    }
    return `Unhealthy · ${at}${
      health.last_health_error ? ` · ${health.last_health_error}` : ""
    }`;
  })();

  return (
    <span className="inline-flex items-center gap-1.5 align-middle">
      <span
        className={dotClasses}
        title={tooltip}
        aria-label={tooltip}
      />
      <button
        type="button"
        onClick={recheck}
        disabled={busy}
        className={cn(
          "text-xs px-1 py-0.5 rounded",
          "text-on-surface-variant hover:text-on-surface",
          "hover:bg-surface-variant/30",
          "disabled:opacity-50",
        )}
        title="Recheck now"
      >
        {busy ? "…" : "↻"}
      </button>
    </span>
  );
}
