"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { useToast } from "@/components/Toast";
import { login, registerUser } from "@/lib/api";

export default function RegisterPage() {
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
      await registerUser(email, password);
      await login(email, password);
      router.push("/chat");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Registration failed";
      // `api()` throws `${status} ${statusText}: ${detail}`. A 409 is
      // the "email already registered" case — the user can fix this
      // by editing the form, so keep it inline. Everything else
      // (network, 5xx) goes to a toast.
      if (/^409\b/.test(msg)) {
        setError("Email already registered.");
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
        <h1 className="font-headline text-2xl mb-4">Create account</h1>
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
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="password (≥8 chars)"
            className="w-full rounded-xl bg-surface-container-high/60 px-4 py-2 text-on-surface border border-outline/20 focus:outline-none focus:border-primary"
          />
          {error ? <div className="text-error text-sm">{error}</div> : null}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-primary-container text-on-primary-container py-2 font-semibold hover:opacity-90 disabled:opacity-50"
          >
            {loading ? "Creating..." : "Create account"}
          </button>
        </form>
        <div className="mt-4 text-sm text-on-surface-variant">
          Have an account?{" "}
          <Link href="/login" className="text-primary underline">
            Sign in
          </Link>
        </div>
      </GlassPanel>
    </main>
  );
}
