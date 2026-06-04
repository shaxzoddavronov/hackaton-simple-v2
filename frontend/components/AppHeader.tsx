"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { LocaleSwitcher } from "@/components/LocaleSwitcher";
import { clearToken, getToken } from "@/lib/api";
import { useT } from "@/lib/i18n/context";


export function AppHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const t = useT();
  const [isSuperuser, setIsSuperuser] = useState(false);
  // Track auth + auth-page status in state so we can use them in
  // hook deps without ever calling hooks conditionally. React's
  // rules-of-hooks require every hook to fire in the same order
  // on every render — so the early return must come AFTER every
  // hook call, not before.
  const onAuthPage =
    pathname === "/login" || pathname === "/register";
  const authed =
    typeof window !== "undefined" && Boolean(getToken());

  // Phase 16 — pull the user's role so the Admin link only shows
  // for super-users. Skipped when we're on an auth page (we're
  // about to bail) or when no token is present.
  useEffect(() => {
    if (onAuthPage || !authed) return;
    const base =
      process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8080";
    fetch(`${base}/auth/me`, {
      headers: {
        Authorization: `Bearer ${localStorage.getItem("qm.token") || ""}`,
      },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data.is_superuser === "boolean") {
          setIsSuperuser(data.is_superuser);
        }
      })
      .catch(() => {
        /* not admin, stay hidden */
      });
  }, [authed, pathname, onAuthPage]);

  // Header isn't useful on auth pages — bail AFTER all hooks have
  // fired so the hook ordering stays stable across renders.
  if (onAuthPage) return null;

  function signOut() {
    clearToken();
    router.push("/login");
  }

  const nav = [
    { href: "/", label: t.nav_workspaces },
    { href: "/chat", label: t.nav_chat },
    { href: "/settings", label: t.nav_settings },
    ...(isSuperuser
      ? [{ href: "/admin/users", label: t.nav_admin }]
      : []),
  ];

  // Phase 43 — Neural Dark v2 tokens. The header now sits on
  // bg-0/80 with a backdrop-blur for the sticky-glass effect the
  // design brief calls out for sticky headers / command palette.
  return (
    <header
      className="sticky top-0 z-20 backdrop-blur-xl border-b"
      style={{
        backgroundColor: "color-mix(in srgb, var(--bg-0) 78%, transparent)",
        borderColor: "var(--border-subtle)",
      }}
    >
      <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
        <Link
          href="/"
          className="font-headline text-lg tracking-tight"
          style={{ color: "var(--fg-0)" }}
        >
          QueryMind{" "}
          <span style={{ color: "var(--accent)" }}>AI</span>
        </Link>
        <nav className="flex items-center gap-1">
          {nav.map((n) => {
            const active =
              pathname === n.href ||
              (n.href !== "/" && pathname.startsWith(n.href));
            return (
              <Link
                key={n.href}
                href={n.href}
                className={
                  "px-3 py-1.5 rounded-lg text-sm transition-colors " +
                  (active ? "is-active" : "is-inactive")
                }
                style={
                  active
                    ? {
                        backgroundColor: "var(--accent-wash)",
                        color: "var(--accent)",
                      }
                    : { color: "var(--fg-2)" }
                }
              >
                {n.label}
              </Link>
            );
          })}
          <span className="mx-2">
            <LocaleSwitcher />
          </span>
          {authed ? (
            <button
              onClick={signOut}
              className="px-3 py-1.5 rounded-lg text-sm transition-colors"
              style={{ color: "var(--fg-2)" }}
              onMouseEnter={(e) =>
                (e.currentTarget.style.color = "var(--status-error)")
              }
              onMouseLeave={(e) =>
                (e.currentTarget.style.color = "var(--fg-2)")
              }
            >
              {t.nav_sign_out}
            </button>
          ) : (
            <Link
              href="/login"
              className="px-3 py-1.5 rounded-lg text-sm"
              style={{
                backgroundColor: "var(--accent-wash)",
                color: "var(--accent)",
              }}
            >
              {t.nav_sign_in}
            </Link>
          )}
        </nav>
      </div>
    </header>
  );
}
