"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { GlassPanel } from "@/components/GlassPanel";
import { MessageBubble } from "@/components/MessageBubble";
import { useToast } from "@/components/Toast";
import {
  api,
  deleteSession,
  getToken,
  listConnections,
  listSessions,
  loadSession,
  streamChat,
  type ChatSessionSummary,
  type ConnectionSummary,
} from "@/lib/api";
import type { ChatMessage, Citation, UISpec } from "@/lib/types";

type WorkspaceOut = {
  id: string;
  name: string;
  status: string;
  connection_count: number;
};

const ACTIVE_WS_KEY = "qm_active_workspace";
const ACTIVE_CONN_KEY = "qm_active_connection";

export default function ChatPage() {
  const router = useRouter();
  const search = useSearchParams();
  const toast = useToast();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [authMissing, setAuthMissing] = useState(false);
  const [activeNode, setActiveNode] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceOut[] | null>(null);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(
    null,
  );
  const [connections, setConnections] = useState<ConnectionSummary[] | null>(
    null,
  );
  const [activeConnectionId, setActiveConnectionId] = useState<string | null>(
    null,
  );
  const [history, setHistory] = useState<ChatSessionSummary[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // ── Init: pick active workspace from ?workspace= or localStorage ──
  useEffect(() => {
    if (!getToken()) {
      setAuthMissing(true);
      return;
    }
    api<WorkspaceOut[]>("/workspaces")
      .then((items) => {
        setWorkspaces(items);
        const fromUrl = search.get("workspace");
        const fromStorage =
          typeof window !== "undefined"
            ? window.localStorage.getItem(ACTIVE_WS_KEY)
            : null;
        const ready = items.find((w) => w.status === "ready");
        const candidate =
          (fromUrl && items.find((w) => w.id === fromUrl)?.id) ||
          (fromStorage && items.find((w) => w.id === fromStorage)?.id) ||
          ready?.id ||
          items[0]?.id ||
          null;
        if (candidate) {
          setActiveWorkspaceId(candidate);
          window.localStorage.setItem(ACTIVE_WS_KEY, candidate);
        }
      })
      .catch(() => setWorkspaces([]));
  }, [search]);

  // ── If ?session= is in the URL, eagerly load that thread ──
  // We wait until we know the workspace so the session list reflects it.
  useEffect(() => {
    if (!activeWorkspaceId) return;
    const sid = search.get("session");
    if (!sid || sid === sessionId) return;
    void openSession(sid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeWorkspaceId, search]);

  // ── Load connections whenever workspace changes ──
  useEffect(() => {
    if (!activeWorkspaceId) {
      setConnections(null);
      setActiveConnectionId(null);
      return;
    }
    listConnections(activeWorkspaceId)
      .then((items) => {
        setConnections(items);
        const fromUrl = search.get("connection");
        const fromStorage =
          typeof window !== "undefined"
            ? window.localStorage.getItem(ACTIVE_CONN_KEY)
            : null;
        const ready = items.find((c) => c.status === "ready");
        const candidate =
          (fromUrl && items.find((c) => c.id === fromUrl)?.id) ||
          (fromStorage && items.find((c) => c.id === fromStorage)?.id) ||
          ready?.id ||
          items[0]?.id ||
          null;
        setActiveConnectionId(candidate);
        if (candidate && typeof window !== "undefined") {
          window.localStorage.setItem(ACTIVE_CONN_KEY, candidate);
        }
      })
      .catch(() => setConnections([]));
  }, [activeWorkspaceId, search]);

  // ── Refresh the sidebar whenever workspace changes or after a turn ──
  const refreshHistory = useCallback(async () => {
    if (!activeWorkspaceId) {
      setHistory([]);
      return;
    }
    try {
      const items = await listSessions(activeWorkspaceId);
      setHistory(items);
      setHistoryError(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Load failed";
      setHistoryError(msg);
      toast.error(`Couldn't load chat history: ${msg}`);
    }
  }, [activeWorkspaceId, toast]);

  useEffect(() => {
    void refreshHistory();
  }, [refreshHistory]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, activeNode]);

  function pickWorkspace(id: string) {
    setActiveWorkspaceId(id);
    setSessionId(null);
    setMessages([]);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(ACTIVE_WS_KEY, id);
    }
    // Drop the ?session= part from the URL when switching workspaces.
    router.replace(`/chat?workspace=${id}`);
  }

  async function openSession(id: string) {
    try {
      const detail = await loadSession(id);
      // The session may live under a different workspace if the user
      // followed a deep link — sync the active workspace to match.
      if (detail.workspace_id && detail.workspace_id !== activeWorkspaceId) {
        setActiveWorkspaceId(detail.workspace_id);
        window.localStorage.setItem(ACTIVE_WS_KEY, detail.workspace_id);
      }
      setSessionId(detail.session_id);
      setMessages(
        detail.messages.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          ui_spec: (m.ui_spec ?? null) as UISpec | null,
        })),
      );
      router.replace(
        `/chat?workspace=${detail.workspace_id ?? activeWorkspaceId ?? ""}&session=${detail.session_id}`,
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Load failed";
      setHistoryError(msg);
      toast.error(`Couldn't open chat session: ${msg}`);
    }
  }

  function newSession() {
    setSessionId(null);
    setMessages([]);
    if (activeWorkspaceId) {
      router.replace(`/chat?workspace=${activeWorkspaceId}`);
    }
  }

  async function removeSession(id: string) {
    if (!window.confirm("Sessiyani o'chirib yuborilsinmi?")) return;
    try {
      await deleteSession(id);
      if (id === sessionId) {
        newSession();
      }
      await refreshHistory();
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : "Delete failed");
    }
  }

  if (authMissing) {
    return (
      <main className="mx-auto max-w-2xl px-4 py-16">
        <GlassPanel className="px-6 py-6 text-on-surface">
          You need to{" "}
          <a className="text-primary underline" href="/login">
            sign in
          </a>{" "}
          before chatting.
        </GlassPanel>
      </main>
    );
  }

  async function send() {
    if (!input.trim() || streaming) return;
    if (!activeWorkspaceId) {
      setMessages((m) => [
        ...m,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: "",
          ui_spec: {
            type: "text_only",
            body_md:
              "Workspace tanlanmagan. Yuqoridagi tanlovdan birini tanlang yoki avval **Connect database** orqali yarating.",
          },
        },
      ]);
      return;
    }
    const userText = input.trim();
    setInput("");
    setMessages((m) => [
      ...m,
      { id: crypto.randomUUID(), role: "user", content: userText },
    ]);
    setStreaming(true);
    setActiveNode(null);

    let finalSpec: UISpec | null = null;
    let finalSql: string | null = null;
    let finalSubResults: Record<
      string,
      { columns: string[]; row_count: number }
    > | null = null;
    let finalCitations: Citation[] | null = null;
    let assistantId = crypto.randomUUID();
    let newlyCreatedSessionId: string | null = null;

    try {
      await streamChat(
        {
          message: userText,
          session_id: sessionId,
          active_workspace_id: activeWorkspaceId,
          active_connection_id: activeConnectionId,
        },
        (evt) => {
          if (evt.event === "session" && evt.data && typeof evt.data === "object") {
            const d = evt.data as { session_id?: string };
            if (d.session_id) {
              if (!sessionId) newlyCreatedSessionId = d.session_id;
              setSessionId(d.session_id);
            }
          } else if (evt.event === "node" && evt.data && typeof evt.data === "object") {
            const d = evt.data as { node?: string };
            if (d.node) setActiveNode(d.node);
          } else if (evt.event === "final" && evt.data && typeof evt.data === "object") {
            const d = evt.data as {
              ui_spec?: UISpec | null;
              sql?: string | null;
              assistant_message_id?: string;
              sub_results?: Record<
                string,
                { columns: string[]; row_count: number }
              > | null;
              citations?: Citation[] | null;
            };
            finalSpec = d.ui_spec ?? null;
            finalSql = d.sql ?? null;
            // Federated turns carry per-sub-query breakdown; single-DB
            // turns return {} which we treat as absent.
            if (d.sub_results && Object.keys(d.sub_results).length > 0) {
              finalSubResults = d.sub_results;
            }
            if (d.citations && d.citations.length > 0) {
              finalCitations = d.citations;
            }
            if (d.assistant_message_id) assistantId = d.assistant_message_id;
          } else if (evt.event === "error") {
            const d = evt.data as { message?: string };
            finalSpec = {
              type: "text_only",
              body_md: `⚠️ ${d?.message ?? "Stream failed."}`,
            };
          }
        },
      );
    } catch (err) {
      finalSpec = {
        type: "text_only",
        body_md: `⚠️ ${err instanceof Error ? err.message : "Stream failed."}`,
      };
    } finally {
      setStreaming(false);
      setActiveNode(null);
      setMessages((m) => [
        ...m,
        {
          id: assistantId,
          role: "assistant",
          content: "",
          ui_spec: finalSpec,
          sql: finalSql,
          sub_results: finalSubResults,
          citations: finalCitations,
        },
      ]);
      // After the first message of a brand-new session, sync the URL
      // so refresh / back-button keeps the thread open.
      if (newlyCreatedSessionId && activeWorkspaceId) {
        router.replace(
          `/chat?workspace=${activeWorkspaceId}&session=${newlyCreatedSessionId}`,
        );
      }
      void refreshHistory();
    }
  }

  const activeWs =
    workspaces?.find((w) => w.id === activeWorkspaceId) ?? null;

  return (
    <main className="mx-auto max-w-6xl px-4 py-8 h-screen flex gap-4">
      {/* ── Sidebar: chat history ── */}
      <aside className="w-72 shrink-0 flex flex-col">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-headline text-on-surface text-lg">Chats</h2>
          <button
            type="button"
            onClick={newSession}
            className="text-xs rounded-lg bg-primary-container/40 text-primary px-2 py-1 hover:opacity-90"
          >
            + New
          </button>
        </div>
        <GlassPanel className="flex-1 overflow-y-auto px-2 py-2 space-y-1">
          {historyError ? (
            <div className="text-error text-xs px-2 py-2">{historyError}</div>
          ) : history === null ? (
            <div className="text-on-surface-variant text-xs px-2 py-2">
              Loading…
            </div>
          ) : history.length === 0 ? (
            <div className="text-on-surface-variant text-xs px-2 py-2">
              Hozircha hech qanday chat yo&apos;q.
            </div>
          ) : (
            history.map((s) => {
              const selected = s.id === sessionId;
              return (
                <div
                  key={s.id}
                  className={
                    "group flex items-center gap-1 rounded-lg px-2 py-2 " +
                    (selected
                      ? "bg-primary-container/30"
                      : "hover:bg-surface-container-high/40")
                  }
                >
                  <button
                    type="button"
                    onClick={() => void openSession(s.id)}
                    className="flex-1 text-left"
                  >
                    <div className="text-on-surface text-sm truncate">
                      {s.title}
                    </div>
                    <div className="text-on-surface-variant text-xs">
                      {new Date(s.last_message_at).toLocaleString("uz-UZ", {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      void removeSession(s.id);
                    }}
                    className="opacity-0 group-hover:opacity-100 text-on-surface-variant hover:text-error px-1 text-xs"
                    title="Delete"
                  >
                    ✕
                  </button>
                </div>
              );
            })
          )}
        </GlassPanel>
      </aside>

      {/* ── Main chat column ── */}
      <section className="flex-1 flex flex-col min-w-0">
        <header className="mb-4 space-y-3">
          <div className="flex items-end justify-between gap-4">
            <div>
              <h1 className="font-headline text-2xl text-on-surface">
                Neural Chat
              </h1>
              <p className="text-on-surface-variant text-sm">
                Ask anything about your connected databases.
              </p>
            </div>
            {workspaces && workspaces.length > 0 ? (
              <div className="flex items-end gap-3">
                <label className="flex flex-col text-xs text-on-surface-variant gap-1">
                  <span className="uppercase tracking-wider">Workspace</span>
                  <select
                    value={activeWorkspaceId ?? ""}
                    onChange={(e) => pickWorkspace(e.target.value)}
                    className="rounded-lg bg-surface-container-high/60 px-3 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
                  >
                    {workspaces.map((w) => (
                      <option key={w.id} value={w.id}>
                        {w.name}
                        {w.status !== "ready" ? ` · ${w.status}` : ""}
                      </option>
                    ))}
                  </select>
                </label>
                {connections && connections.length > 0 ? (
                  <label className="flex flex-col text-xs text-on-surface-variant gap-1">
                    <span className="uppercase tracking-wider">Database</span>
                    <select
                      value={activeConnectionId ?? ""}
                      onChange={(e) => {
                        setActiveConnectionId(e.target.value);
                        window.localStorage.setItem(
                          ACTIVE_CONN_KEY,
                          e.target.value,
                        );
                      }}
                      className="rounded-lg bg-surface-container-high/60 px-3 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
                    >
                      {connections.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name} ({c.dialect}
                          {c.status !== "ready" ? ` · ${c.status}` : ""})
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
              </div>
            ) : null}
          </div>
          {activeWs && activeWs.status !== "ready" ? (
            <GlassPanel className="px-4 py-2 text-on-surface-variant text-sm">
              Workspace status: <b>{activeWs.status}</b> — profiling tugashini
              kuting yoki{" "}
              <a href="/" className="text-primary underline">
                workspaces
              </a>{" "}
              ga qayting.
            </GlassPanel>
          ) : null}
          {workspaces && workspaces.length === 0 ? (
            <GlassPanel className="px-4 py-3 text-on-surface-variant text-sm">
              Hech bir workspace yo&apos;q. Avval{" "}
              <a className="text-primary underline" href="/workspaces/new">
                New workspace
              </a>{" "}
              orqali yarating, keyin uning ichida database connection
              qo&apos;shing.
            </GlassPanel>
          ) : null}
          {activeWs && connections && connections.length === 0 ? (
            <GlassPanel className="px-4 py-3 text-on-surface-variant text-sm">
              Bu workspace ichida hali database yo&apos;q.{" "}
              <a
                className="text-primary underline"
                href={`/workspaces/${activeWs.id}`}
              >
                Connection qo&apos;shing
              </a>
              .
            </GlassPanel>
          ) : null}
        </header>

        <div className="flex-1 overflow-y-auto space-y-4 pr-2">
          {messages.map((m, i) => {
            // For each assistant message, look back to find the
            // immediately preceding user prompt so the Star button
            // can save it. If there's no user message ahead of
            // this one (e.g. workspace-clarify auto-response),
            // pass undefined and the button hides.
            let previousUserPrompt: string | undefined;
            if (m.role === "assistant") {
              for (let j = i - 1; j >= 0; j--) {
                const prev = messages[j];
                if (prev && prev.role === "user") {
                  previousUserPrompt = prev.content;
                  break;
                }
              }
            }
            return (
              <MessageBubble
                key={m.id}
                message={m}
                previousUserPrompt={previousUserPrompt}
                workspaceId={activeWorkspaceId}
                connectionId={activeConnectionId}
              />
            );
          })}
          {streaming ? (
            <div className="text-on-surface-variant text-sm italic">
              {activeNode ? `running ${activeNode}…` : "thinking…"}
            </div>
          ) : null}
          <div ref={endRef} />
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
          className="mt-4 flex gap-2"
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              activeConnectionId
                ? `Ask about ${
                    connections?.find((c) => c.id === activeConnectionId)
                      ?.name ?? "the connected DB"
                  }…`
                : "Ask a question…"
            }
            className="flex-1 rounded-xl bg-surface-container-high/60 px-4 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
            disabled={streaming || !activeConnectionId}
          />
          <button
            type="submit"
            disabled={
              streaming || !input.trim() || !activeConnectionId
            }
            className="rounded-xl bg-primary-container text-on-primary-container px-4 py-2 font-semibold disabled:opacity-50"
          >
            Send
          </button>
        </form>
      </section>
    </main>
  );
}
