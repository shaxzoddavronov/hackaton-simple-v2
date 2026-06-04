"use client";

/**
 * Phase 16 + Phase 43 — admin-only account creation, Neural Dark v2 styled.
 */
import Link from "next/link";

import { GlassPanel } from "@/components/GlassPanel";


export default function RegisterPage() {
  return (
    <main className="mx-auto max-w-md p-8 flex flex-col gap-4">
      <GlassPanel className="p-7 flex flex-col gap-3">
        <div className="qm-overline" style={{ color: "var(--fg-2)" }}>
          QueryMind AI
        </div>
        <h1 className="qm-h1">Account creation is admin-only</h1>
        <p
          className="qm-body-sm leading-relaxed"
          style={{ color: "var(--fg-2)" }}
        >
          QueryMind is a self-hosted analytics tool — your administrator
          provisions accounts inside the team. There is no public
          sign-up.
        </p>
        <p
          className="qm-body-sm leading-relaxed"
          style={{ color: "var(--fg-2)" }}
        >
          Already have credentials? Sign in instead.
        </p>
        <div className="flex gap-2 pt-1">
          <Link
            href="/login"
            className="px-4 py-2 rounded-[10px] qm-body-sm font-semibold"
            style={{
              backgroundColor: "var(--accent)",
              color: "var(--on-accent)",
            }}
          >
            Go to sign in
          </Link>
        </div>
      </GlassPanel>
      <GlassPanel
        className="p-4 qm-caption"
        style={{ color: "var(--fg-2)" }}
      >
        <strong style={{ color: "var(--fg-0)" }}>Administrators:</strong>{" "}
        new users are created at{" "}
        <code
          className="font-mono qm-code"
          style={{ color: "var(--fg-0)" }}
        >
          /admin/users
        </code>{" "}
        while signed in as a super-user. The bootstrap super-user is
        seeded from{" "}
        <code
          className="font-mono qm-code"
          style={{ color: "var(--fg-0)" }}
        >
          QM_BOOTSTRAP_SUPERUSER_*
        </code>{" "}
        env vars on first startup.
      </GlassPanel>
    </main>
  );
}
