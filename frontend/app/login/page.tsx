"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
import { login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const toast = useToast();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await login(email, password);
      router.push("/chat");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Login failed";
      // `login()` throws `Login failed: <status>`. A 401/403 means
      // wrong credentials — keep the inline field-level hint so the
      // user fixes it right there. Everything else (network down,
      // backend 5xx, fetch TypeError) becomes a toast.
      if (/\b40[13]\b/.test(msg)) {
        setError("Invalid email or password.");
      } else {
        toast.error(msg);
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="mx-auto max-w-md px-4 py-16">
      <GlassPanel className="px-6 py-6">
        <h1 className="font-headline text-2xl mb-4">Sign in</h1>
        <form onSubmit={submit} className="space-y-3">
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="email"
            className="w-full rounded-xl bg-surface-container-high/60 px-4 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
          />
          <input
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="password"
            className="w-full rounded-xl bg-surface-container-high/60 px-4 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
          />
          {error ? <div className="text-error text-sm">{error}</div> : null}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-primary-container text-on-primary-container py-2 font-semibold hover:opacity-90 disabled:opacity-50"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </form>
        <div className="mt-4 text-sm text-on-surface-variant">
          New here?{" "}
          <Link href="/register" className="text-primary underline">
            Create an account
          </Link>
        </div>
      </GlassPanel>
    </main>
  );
}
