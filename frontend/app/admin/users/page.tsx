"use client";

/**
 * Phase 16 — super-user-only user management.
 *
 * Lists every user with their role and active-state. Inline
 * actions: activate / deactivate, promote / demote, set password,
 * delete. Self-rows surface read-only badges for the guarded
 * actions (cannot self-demote / self-deactivate / self-delete) so
 * the UI matches the backend's 400 responses.
 */
import { useCallback, useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
import {
  createAdminUser,
  deleteAdminUser,
  listAdminUsers,
  updateAdminUser,
  type AdminUser,
} from "@/lib/api";
import { cn } from "@/lib/cn";


export default function AdminUsersPage() {
  const [me, setMe] = useState<{ id: string } | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const toast = useToast();

  const reload = useCallback(async () => {
    setError(null);
    try {
      const rows = await listAdminUsers();
      setUsers(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load users");
    }
  }, []);

  useEffect(() => {
    // Find out who we are so self-row guards work.
    fetch("/api/me-cookie-shim", { cache: "no-store" }).catch(() => {});
    void (async () => {
      try {
        const r = await fetch(
          `${process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8080"}/auth/me`,
          {
            headers: {
              Authorization: `Bearer ${
                typeof window !== "undefined"
                  ? localStorage.getItem("qm.token") || ""
                  : ""
              }`,
            },
          },
        );
        if (r.ok) {
          const data = (await r.json()) as { id: string };
          setMe({ id: data.id });
        }
      } catch {
        /* ignore */
      }
    })();
    void reload();
  }, [reload]);

  async function onToggleActive(u: AdminUser) {
    setBusy(u.id);
    try {
      const next = await updateAdminUser(u.id, { is_active: !u.is_active });
      setUsers((cur) =>
        (cur || []).map((x) => (x.id === u.id ? next : x)),
      );
      toast.success(
        `${u.username} ${next.is_active ? "activated" : "deactivated"}`,
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusy(null);
    }
  }

  async function onToggleSuperuser(u: AdminUser) {
    setBusy(u.id);
    try {
      const next = await updateAdminUser(u.id, {
        is_superuser: !u.is_superuser,
      });
      setUsers((cur) =>
        (cur || []).map((x) => (x.id === u.id ? next : x)),
      );
      toast.success(
        `${u.username} ${
          next.is_superuser ? "promoted to admin" : "demoted"
        }`,
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusy(null);
    }
  }

  async function onResetPassword(u: AdminUser) {
    const pw = window.prompt(
      `New password for ${u.username} (min 8 chars, must include a digit):`,
    );
    if (!pw) return;
    setBusy(u.id);
    try {
      await updateAdminUser(u.id, { password: pw });
      toast.success(
        `Password rotated for ${u.username}. Their refresh tokens were revoked.`,
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Password reset failed");
    } finally {
      setBusy(null);
    }
  }

  async function onDelete(u: AdminUser) {
    if (
      !window.confirm(
        `Delete ${u.username}? This also drops their workspaces, sessions and refresh tokens — non-reversible.`,
      )
    )
      return;
    setBusy(u.id);
    try {
      await deleteAdminUser(u.id);
      setUsers((cur) => (cur || []).filter((x) => x.id !== u.id));
      toast.success(`Deleted ${u.username}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setBusy(null);
    }
  }

  if (error) {
    return (
      <div className="p-6">
        <GlassPanel className="p-5 text-rose-400">{error}</GlassPanel>
      </div>
    );
  }
  if (!users) {
    return (
      <div className="p-6 text-nd-fg-2">Loading users…</div>
    );
  }

  return (
    <div className="p-6 space-y-4 max-w-5xl">
      <div className="flex items-baseline justify-between">
        <h1 className="font-headline text-2xl text-nd-fg-0">Users</h1>
        <button
          type="button"
          onClick={() => setShowCreate((v) => !v)}
          className="px-3 py-1.5 rounded-xl bg-nd-accent-wash text-nd-accent text-sm"
        >
          {showCreate ? "✕ Cancel" : "+ New user"}
        </button>
      </div>

      {showCreate ? (
        <CreateUserForm
          onCreated={(u) => {
            setUsers((cur) => [u, ...(cur || [])]);
            setShowCreate(false);
            toast.success(`Created ${u.username}`);
          }}
        />
      ) : null}

      <GlassPanel className="overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-nd-bg-1">
            <tr className="text-left text-nd-fg-2 text-xs uppercase tracking-wider">
              <th className="p-3">Username</th>
              <th className="p-3">Email</th>
              <th className="p-3">Role</th>
              <th className="p-3">Status</th>
              <th className="p-3">Created</th>
              <th className="p-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const isMe = me?.id === u.id;
              return (
                <tr
                  key={u.id}
                  className="border-t border-nd-border-subtle align-middle"
                >
                  <td className="p-3 font-mono">{u.username}</td>
                  <td className="p-3 text-nd-fg-2">{u.email}</td>
                  <td className="p-3">
                    <span
                      className={cn(
                        "px-2 py-0.5 text-xs uppercase tracking-wider rounded",
                        u.is_superuser
                          ? "bg-amber-500/15 text-amber-300"
                          : "bg-nd-bg-1 text-nd-fg-2",
                      )}
                    >
                      {u.is_superuser ? "admin" : "user"}
                    </span>
                  </td>
                  <td className="p-3">
                    <span
                      className={cn(
                        "px-2 py-0.5 text-xs uppercase tracking-wider rounded",
                        u.is_active
                          ? "bg-emerald-500/15 text-emerald-300"
                          : "bg-rose-500/15 text-rose-300",
                      )}
                    >
                      {u.is_active ? "active" : "disabled"}
                    </span>
                  </td>
                  <td className="p-3 text-xs text-nd-fg-2">
                    {new Date(u.created_at).toLocaleDateString()}
                  </td>
                  <td className="p-3">
                    <div className="flex gap-1 justify-end flex-wrap">
                      <button
                        type="button"
                        disabled={busy === u.id || isMe}
                        onClick={() => onToggleActive(u)}
                        title={
                          isMe
                            ? "You cannot deactivate your own account"
                            : ""
                        }
                        className="text-xs px-2 py-1 rounded bg-nd-bg-1 hover:bg-nd-bg-hover disabled:opacity-40"
                      >
                        {u.is_active ? "Disable" : "Activate"}
                      </button>
                      <button
                        type="button"
                        disabled={busy === u.id || isMe}
                        onClick={() => onToggleSuperuser(u)}
                        title={
                          isMe ? "You cannot demote yourself" : ""
                        }
                        className="text-xs px-2 py-1 rounded bg-nd-bg-1 hover:bg-nd-bg-hover disabled:opacity-40"
                      >
                        {u.is_superuser ? "Demote" : "Promote"}
                      </button>
                      <button
                        type="button"
                        disabled={busy === u.id}
                        onClick={() => onResetPassword(u)}
                        className="text-xs px-2 py-1 rounded bg-nd-bg-1 hover:bg-nd-bg-hover disabled:opacity-40"
                      >
                        Reset password
                      </button>
                      <button
                        type="button"
                        disabled={busy === u.id || isMe}
                        onClick={() => onDelete(u)}
                        title={
                          isMe ? "You cannot delete your own account" : ""
                        }
                        className="text-xs px-2 py-1 rounded bg-rose-500/15 text-rose-300 hover:bg-rose-500/25 disabled:opacity-40"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </GlassPanel>
    </div>
  );
}


function CreateUserForm({
  onCreated,
}: {
  onCreated: (u: AdminUser) => void;
}) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isSuper, setIsSuper] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      const u = await createAdminUser({
        username: username.trim(),
        email: email.trim(),
        password,
        is_superuser: isSuper,
      });
      onCreated(u);
      setUsername("");
      setEmail("");
      setPassword("");
      setIsSuper(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Create failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <GlassPanel className="p-4">
      <form onSubmit={submit} className="grid grid-cols-2 gap-3 text-sm">
        <label className="block space-y-1 col-span-1">
          <span className="text-xs uppercase tracking-wider text-nd-fg-2">
            Username
          </span>
          <input
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            pattern="[A-Za-z0-9_.-]{3,64}"
            placeholder="kamola"
            className="w-full input"
          />
        </label>
        <label className="block space-y-1 col-span-1">
          <span className="text-xs uppercase tracking-wider text-nd-fg-2">
            Email
          </span>
          <input
            required
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="kamola@example.com"
            className="w-full input"
          />
        </label>
        <label className="block space-y-1 col-span-1">
          <span className="text-xs uppercase tracking-wider text-nd-fg-2">
            Password
          </span>
          <input
            required
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="≥ 8 chars · ≥ 1 digit"
            className="w-full input"
          />
        </label>
        <label className="flex items-center gap-2 col-span-1 mt-5">
          <input
            type="checkbox"
            checked={isSuper}
            onChange={(e) => setIsSuper(e.target.checked)}
          />
          <span>Grant super-user role</span>
        </label>
        {err ? (
          <div className="col-span-2 text-xs text-rose-400">{err}</div>
        ) : null}
        <div className="col-span-2 flex justify-end">
          <button
            type="submit"
            disabled={submitting}
            className="px-3 py-1.5 rounded-xl bg-nd-accent-wash text-nd-accent text-sm disabled:opacity-50"
          >
            {submitting ? "Creating…" : "Create user"}
          </button>
        </div>
      </form>
    </GlassPanel>
  );
}
