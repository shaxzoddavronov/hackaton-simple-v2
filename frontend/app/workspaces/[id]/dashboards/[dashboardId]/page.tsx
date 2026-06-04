"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { RenderSpec } from "@/components/RenderSpec";
import { useToast } from "@/components/Toast";
import {
  deleteSavedQuestion,
  getDashboard,
  getToken,
  listSavedQuestions,
  streamChat,
  type DashboardDetail,
  type SavedQuestion,
} from "@/lib/api";
import type { UISpec } from "@/lib/types";

/**
 * Dashboard detail — renders each saved question as a card that
 * replays through the existing /chat SSE pipeline. The agent stack
 * (planner, validator, executor, retriever, citations) IS the
 * dashboard's rendering engine; no parallel path.
 *
 * ``[dashboardId]`` is the dashboard UUID, OR the literal "inbox"
 * for un-filed starred questions.
 */
export default function DashboardDetailPage() {
  const params = useParams<{ id: string; dashboardId: string }>();
  const router = useRouter();
  const toast = useToast();
  const workspaceId = params.id;
  const dashboardId = params.dashboardId;
  const isInbox = dashboardId === "inbox";

  const [data, setData] = useState<{
    name: string;
    description: string | null;
    questions: SavedQuestion[];
  } | null>(null);
  // Per-question render state: keyed by question id.
  const [renders, setRenders] = useState<
    Record<
      string,
      { busy: boolean; spec: UISpec | null; sql: string | null }
    >
  >({});

  const refresh = useCallback(async () => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    try {
      if (isInbox) {
        const all = await listSavedQuestions(workspaceId);
        setData({
          name: "⭐ Inbox",
          description: "Starred questions not yet filed.",
          questions: all.filter((q) => !q.dashboard_id),
        });
      } else {
        const detail: DashboardDetail = await getDashboard(
          workspaceId,
          dashboardId,
        );
        setData({
          name: detail.name,
          description: detail.description,
          questions: detail.questions,
        });
      }
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to load dashboard",
      );
    }
  }, [workspaceId, dashboardId, isInbox, router, toast]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function rerunQuestion(q: SavedQuestion): Promise<void> {
    setRenders((r) => ({
      ...r,
      [q.id]: { busy: true, spec: null, sql: null },
    }));
    let finalSpec: UISpec | null = null;
    let finalSql: string | null = null;
    try {
      await streamChat(
        {
          message: q.prompt,
          active_workspace_id: workspaceId,
          active_connection_id: q.connection_id ?? undefined,
        },
        (evt) => {
          if (
            evt.event === "final" &&
            evt.data &&
            typeof evt.data === "object"
          ) {
            const d = evt.data as {
              ui_spec?: UISpec | null;
              sql?: string | null;
            };
            finalSpec = d.ui_spec ?? null;
            finalSql = d.sql ?? null;
          } else if (evt.event === "error") {
            const d = evt.data as { message?: string };
            finalSpec = {
              type: "text_only",
              body_md: `⚠️ ${d?.message ?? "Stream failed."}`,
            };
          }
        },
      );
    } catch (e) {
      finalSpec = {
        type: "text_only",
        body_md: `⚠️ ${e instanceof Error ? e.message : "Stream failed."}`,
      };
    } finally {
      setRenders((r) => ({
        ...r,
        [q.id]: { busy: false, spec: finalSpec, sql: finalSql },
      }));
    }
  }

  async function runAll(): Promise<void> {
    if (!data) return;
    // Sequential rather than parallel — the LLM has finite
    // concurrency and a dashboard with 10 cards fan-shotting
    // 10 simultaneous /chat sessions would queue up against
    // vLLM anyway. Sequential keeps the UI responsive and the
    // backend log readable.
    for (const q of data.questions) {
      await rerunQuestion(q);
    }
  }

  async function onUnstar(questionId: string): Promise<void> {
    if (!window.confirm("Bu savol o'chiriladi.")) return;
    try {
      await deleteSavedQuestion(workspaceId, questionId);
      toast.success("Question removed");
      setRenders((r) => {
        const copy = { ...r };
        delete copy[questionId];
        return copy;
      });
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to delete");
    }
  }

  if (data === null) {
    return (
      <main className="mx-auto max-w-4xl px-4 py-8 space-y-6">
        <GlassPanel className="px-5 py-4 text-nd-fg-2">
          Loading…
        </GlassPanel>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-4xl px-4 py-8 space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <Link
            href={`/workspaces/${workspaceId}/dashboards`}
            className="text-nd-fg-2 text-sm hover:underline"
          >
            ← Dashboards
          </Link>
          <h1 className="font-headline text-headline-lg text-nd-fg-0 mt-2">
            {data.name}
          </h1>
          {data.description ? (
            <p className="text-nd-fg-2 text-sm mt-1">
              {data.description}
            </p>
          ) : null}
        </div>
        {data.questions.length > 0 ? (
          <button
            type="button"
            onClick={() => void runAll()}
            className="rounded-xl bg-nd-accent text-nd-on-accent px-4 py-2 font-semibold"
          >
            Run all
          </button>
        ) : null}
      </header>

      {data.questions.length === 0 ? (
        <GlassPanel className="px-5 py-8 text-center">
          <p className="text-nd-fg-0 mb-2 font-headline text-xl">
            No questions yet.
          </p>
          <p className="text-nd-fg-2">
            Star questions from chat to add them here.
          </p>
        </GlassPanel>
      ) : (
        <div className="space-y-3">
          {data.questions.map((q) => {
            const r = renders[q.id];
            return (
              <GlassPanel key={q.id} className="px-5 py-4 space-y-3">
                <div className="flex items-baseline justify-between gap-4">
                  <div className="flex-1">
                    <div className="font-headline text-nd-fg-0 text-base">
                      {q.title}
                    </div>
                    <p className="text-nd-fg-2 text-sm">
                      {q.prompt}
                    </p>
                  </div>
                  <div className="flex gap-3 text-sm">
                    <button
                      type="button"
                      onClick={() => void rerunQuestion(q)}
                      disabled={r?.busy}
                      className="text-nd-accent hover:underline disabled:opacity-50"
                    >
                      {r?.busy ? "Running…" : "Run"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void onUnstar(q.id)}
                      className="text-nd-error hover:underline"
                    >
                      Remove
                    </button>
                  </div>
                </div>
                {r?.spec ? (
                  <div className="pt-2 border-t border-nd-border-subtle">
                    <RenderSpec spec={r.spec} />
                  </div>
                ) : !r?.busy ? (
                  <div className="text-nd-fg-2 text-xs italic">
                    Click Run to fetch a fresh answer.
                  </div>
                ) : null}
              </GlassPanel>
            );
          })}
        </div>
      )}
    </main>
  );
}
