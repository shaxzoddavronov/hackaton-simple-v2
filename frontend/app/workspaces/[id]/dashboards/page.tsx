"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
import {
  createDashboard,
  deleteDashboard,
  getToken,
  listDashboards,
  listSavedQuestions,
  type Dashboard,
  type SavedQuestion,
} from "@/lib/api";

/**
 * Dashboards index — list of curated boards in this workspace plus the
 * "Inbox" pseudo-dashboard for questions starred but not yet filed.
 */
export default function DashboardsIndexPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const toast = useToast();
  const workspaceId = params.id;
  const [dashboards, setDashboards] = useState<Dashboard[] | null>(null);
  const [inboxCount, setInboxCount] = useState<number>(0);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");

  const refresh = useCallback(async () => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    try {
      const [dbs, inbox] = await Promise.all([
        listDashboards(workspaceId),
        listSavedQuestions(workspaceId),
      ]);
      setDashboards(dbs);
      // Inbox = saved questions with no dashboard_id.
      setInboxCount(inbox.filter((q) => !q.dashboard_id).length);
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to load dashboards",
      );
    }
  }, [workspaceId, router, toast]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    try {
      await createDashboard(workspaceId, {
        name: newName.trim(),
        description: newDescription.trim() || null,
      });
      toast.success("Dashboard created");
      setNewName("");
      setNewDescription("");
      setCreating(false);
      await refresh();
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to create dashboard",
      );
    }
  }

  async function onDelete(dashboardId: string) {
    if (
      !window.confirm(
        "Bu dashboard o'chiriladi. Saqlangan savollar Inbox'ga ko'chadi.",
      )
    ) {
      return;
    }
    try {
      await deleteDashboard(workspaceId, dashboardId);
      toast.success("Dashboard removed");
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to delete");
    }
  }

  return (
    <main className="mx-auto max-w-4xl px-4 py-8 space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <Link
            href={`/workspaces/${workspaceId}`}
            className="text-on-surface-variant text-sm hover:underline"
          >
            ← Workspace
          </Link>
          <h1 className="font-headline text-headline-lg text-on-surface mt-2">
            Dashboards
          </h1>
          <p className="text-on-surface-variant text-sm mt-1">
            Curated collections of starred questions. Each card on a
            dashboard re-runs through the agent on open — fresh answers,
            same prompts.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating((v) => !v)}
          className="rounded-xl bg-primary-container text-on-primary-container px-4 py-2 font-semibold"
        >
          + New dashboard
        </button>
      </header>

      {creating ? (
        <GlassPanel className="px-5 py-4 space-y-3">
          <form onSubmit={onCreate} className="space-y-3">
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Name
              </span>
              <input
                required
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Daily ops"
                className="w-full input"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs uppercase tracking-wider text-on-surface-variant">
                Description (optional)
              </span>
              <input
                value={newDescription}
                onChange={(e) => setNewDescription(e.target.value)}
                placeholder="Refund queue, top customers, today's revenue"
                className="w-full input"
              />
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setCreating(false)}
                className="rounded-xl bg-surface-container-high/60 border border-outline/20 text-on-surface px-3 py-1.5 text-sm"
              >
                Cancel
              </button>
              <button
                type="submit"
                className="rounded-xl bg-primary-container text-on-primary-container px-3 py-1.5 text-sm font-semibold"
              >
                Create
              </button>
            </div>
          </form>
        </GlassPanel>
      ) : null}

      <Link
        href={`/workspaces/${workspaceId}/dashboards/inbox`}
        className="block"
      >
        <GlassPanel className="px-5 py-4 hover:bg-surface-container-high/40 transition-colors">
          <div className="flex items-center justify-between">
            <div>
              <div className="font-headline text-on-surface text-lg">
                ⭐ Inbox
              </div>
              <p className="text-on-surface-variant text-sm">
                Starred questions not yet filed under a dashboard.
              </p>
            </div>
            <span className="text-on-surface-variant">
              {inboxCount} question{inboxCount === 1 ? "" : "s"}
            </span>
          </div>
        </GlassPanel>
      </Link>

      {dashboards === null ? (
        <GlassPanel className="px-5 py-4 text-on-surface-variant">
          Loading…
        </GlassPanel>
      ) : dashboards.length === 0 ? (
        <GlassPanel className="px-5 py-8 text-center">
          <p className="text-on-surface mb-2 font-headline text-xl">
            No dashboards yet.
          </p>
          <p className="text-on-surface-variant">
            Star questions from chat (⭐ button on assistant messages),
            then group them into dashboards here.
          </p>
        </GlassPanel>
      ) : (
        <div className="space-y-3">
          {dashboards.map((d) => (
            <GlassPanel key={d.id} className="px-5 py-4">
              <div className="flex items-center justify-between gap-4">
                <Link
                  href={`/workspaces/${workspaceId}/dashboards/${d.id}`}
                  className="flex-1 hover:underline"
                >
                  <div className="font-headline text-on-surface text-lg">
                    {d.name}
                  </div>
                  {d.description ? (
                    <p className="text-on-surface-variant text-sm">
                      {d.description}
                    </p>
                  ) : null}
                  <div className="text-on-surface-variant text-xs uppercase tracking-wider mt-1">
                    {d.question_count} question
                    {d.question_count === 1 ? "" : "s"}
                  </div>
                </Link>
                <button
                  type="button"
                  onClick={() => onDelete(d.id)}
                  className="text-error text-sm hover:underline"
                >
                  Delete
                </button>
              </div>
            </GlassPanel>
          ))}
        </div>
      )}
    </main>
  );
}
