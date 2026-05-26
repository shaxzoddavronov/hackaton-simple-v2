"use client";

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { GlassPanel } from "@/components/GlassPanel";
import { MessageBubble } from "@/components/MessageBubble";
import { api, getToken, streamChat } from "@/lib/api";
import type { ChatMessage, UISpec } from "@/lib/types";

type WorkspaceOut = {
  id: string;
  name: string;
  dialect: string;
  status: string;
};

const ACTIVE_WS_KEY = "qm_active_workspace";

export default function ChatPage() {
  const search = useSearchParams();
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
  const endRef = useRef<HTMLDivElement>(null);

  // Decide which workspace to pre-select. Priority: ?workspace=<id> in the
  // URL (when navigating from the workspaces page) > the last one used in
  // this browser (localStorage) > the first 'ready' workspace from the API.
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

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, activeNode]);

  function pickWorkspace(id: string) {
    setActiveWorkspaceId(id);
    setSessionId(null); // start a fresh session when switching workspaces
    setMessages([]);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(ACTIVE_WS_KEY, id);
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
    let assistantId = crypto.randomUUID();

    try {
      await streamChat(
        {
          message: userText,
          session_id: sessionId,
          active_workspace_id: activeWorkspaceId,
        },
        (evt) => {
          if (evt.event === "session" && evt.data && typeof evt.data === "object") {
            const d = evt.data as { session_id?: string };
            if (d.session_id) setSessionId(d.session_id);
          } else if (evt.event === "node" && evt.data && typeof evt.data === "object") {
            const d = evt.data as { node?: string };
            if (d.node) setActiveNode(d.node);
          } else if (evt.event === "final" && evt.data && typeof evt.data === "object") {
            const d = evt.data as {
              ui_spec?: UISpec | null;
              sql?: string | null;
              assistant_message_id?: string;
            };
            finalSpec = d.ui_spec ?? null;
            finalSql = d.sql ?? null;
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
        },
      ]);
    }
  }

  const activeWs =
    workspaces?.find((w) => w.id === activeWorkspaceId) ?? null;

  return (
    <main className="mx-auto max-w-3xl px-4 py-8 flex flex-col h-screen">
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
            <label className="flex flex-col text-xs text-on-surface-variant gap-1">
              <span className="uppercase tracking-wider">Workspace</span>
              <select
                value={activeWorkspaceId ?? ""}
                onChange={(e) => pickWorkspace(e.target.value)}
                className="rounded-lg bg-surface-container-high/60 px-3 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
              >
                {workspaces.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name} ({w.dialect}
                    {w.status !== "ready" ? ` · ${w.status}` : ""})
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
        {activeWs && activeWs.status !== "ready" ? (
          <GlassPanel className="px-4 py-2 text-on-surface-variant text-sm">
            Workspace status: <b>{activeWs.status}</b> — profiling tugashini
            kuting yoki <a href="/" className="text-primary underline">workspaces</a> ga qayting.
          </GlassPanel>
        ) : null}
        {workspaces && workspaces.length === 0 ? (
          <GlassPanel className="px-4 py-3 text-on-surface-variant text-sm">
            Hech bir workspace yo'q. Avval{" "}
            <a className="text-primary underline" href="/workspaces/new">
              Connect database
            </a>{" "}
            orqali bittasini yarating.
          </GlassPanel>
        ) : null}
      </header>

      <div className="flex-1 overflow-y-auto space-y-4 pr-2">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
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
            activeWs
              ? `Ask about ${activeWs.name}…`
              : "Ask a question…"
          }
          className="flex-1 rounded-xl bg-surface-container-high/60 px-4 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
          disabled={streaming || !activeWorkspaceId}
        />
        <button
          type="submit"
          disabled={streaming || !input.trim() || !activeWorkspaceId}
          className="rounded-xl bg-primary-container text-on-primary-container px-4 py-2 font-semibold disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </main>
  );
}
