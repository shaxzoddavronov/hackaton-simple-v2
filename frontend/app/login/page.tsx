"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
import { login } from "@/lib/api";

/**
 * Phase 43 — Neural Dark v2 login.
 *
 * Phase 16 contract:
 *   - Accepts USERNAME or EMAIL in the same identifier field. The
 *     backend's `_resolve_login_identifier` decides by `@` presence.
 *   - Public registration is gone — no "create an account" link. A
 *     deactivated user (`is_active=false`) gets a generic 401, the
 *     server never reveals which check failed.
 */
export default function LoginPage() {
  const router = useRouter();
  const toast = useToast();
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await login(identifier, password);
      router.push("/");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Login failed";
      if (/\b40[13]\b/.test(msg)) {
        setError("Invalid credentials.");
      } else {
        toast.error(msg);
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="mx-auto max-w-md px-4 py-16">
      <GlassPanel className="p-7">
        {/* Eyebrow */}
        <div className="qm-overline mb-2">QueryMind AI</div>
        <h1 className="qm-h1 mb-1">Sign in</h1>
        <p className="qm-body-sm mb-5" style={{ color: "var(--fg-2)" }}>
          Use your username or email.
        </p>

        <form onSubmit={submit} className="space-y-3">
          <label className="block space-y-1">
            <span
              className="qm-overline block"
              style={{ color: "var(--fg-2)" }}
            >
              Username or email
            </span>
            <input
              required
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              autoComplete="username"
              placeholder="kamola — or kamola@example.com"
              className="input w-full"
            />
          </label>
          <label className="block space-y-1">
            <span
              className="qm-overline block"
              style={{ color: "var(--fg-2)" }}
            >
              Password
            </span>
            <input
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              className="input w-full"
            />
          </label>
          {error ? (
            <div
              className="qm-body-sm"
              style={{ color: "var(--status-error)" }}
            >
              {error}
            </div>
          ) : null}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-[10px] py-2 font-semibold transition-colors disabled:opacity-50"
            style={{
              backgroundColor: "var(--accent)",
              color: "var(--on-accent)",
            }}
            onMouseEnter={(e) => {
              if (!loading)
                e.currentTarget.style.backgroundColor =
                  "var(--accent-hover)";
            }}
            onMouseLeave={(e) => {
              if (!loading)
                e.currentTarget.style.backgroundColor = "var(--accent)";
            }}
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

        {/* Footer note — no public registration */}
        <div
          className="qm-caption mt-5 pt-4"
          style={{
            borderTop: "1px solid var(--border-subtle)",
            color: "var(--fg-2)",
          }}
        >
          No account yet? QueryMind accounts are created by an
          administrator. Ask your team admin to add you.
        </div>
      </GlassPanel>
    </main>
  );
}
