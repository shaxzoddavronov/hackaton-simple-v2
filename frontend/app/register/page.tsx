"use client";

/**
 * Phase 16 — public registration is intentionally gone.
 * QueryMind now creates users only through the admin endpoints
 * (see `/admin/users` and BACKEND_SURFACE.md §1). This page
 * replaces the old self-service form with a polite redirect:
 * "ask your administrator".
 */
import Link from "next/link";

import { GlassPanel } from "@/components/GlassPanel";


export default function RegisterPage() {
  return (
    <main className="mx-auto max-w-md p-8 flex flex-col gap-4">
      <GlassPanel className="p-6 flex flex-col gap-3">
        <h1 className="font-headline text-2xl text-on-surface">
          Account creation is admin-only
        </h1>
        <p className="text-on-surface-variant text-sm leading-relaxed">
          QueryMind is a self-hosted analytics tool — your administrator
          provisions accounts inside the team. There is no public
          sign-up.
        </p>
        <p className="text-on-surface-variant text-sm leading-relaxed">
          Already have credentials? Sign in instead.
        </p>
        <div className="flex gap-2 pt-1">
          <Link
            href="/login"
            className="px-4 py-2 rounded-xl bg-primary-container/40 text-primary text-sm hover:bg-primary-container/60"
          >
            Go to sign in
          </Link>
        </div>
      </GlassPanel>
      <GlassPanel className="p-4 text-xs text-on-surface-variant">
        <strong className="text-on-surface">Administrators:</strong>{" "}
        new users are created at{" "}
        <code className="font-mono">/admin/users</code> while signed in
        as a super-user. The bootstrap super-user is seeded from
        <code className="font-mono">
          {" "}
          QM_BOOTSTRAP_SUPERUSER_*
        </code>{" "}
        env vars on first startup.
      </GlassPanel>
    </main>
  );
}
