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

  function signOut() {
    clearToken();
    router.push("/login");
  }

  // Header isn't useful on auth pages
  if (pathname === "/login" || pathname === "/register") return null;

  const authed = typeof window !== "undefined" && Boolean(getToken());

  // Phase 16 — pull the user's role so the Admin link only shows
  // for super-users. Fire once on mount when authed; falls back
  // to "not admin" on any network glitch.
  useEffect(() => {
    if (!authed) return;
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
  }, [authed, pathname]);

  const nav = [
    { href: "/", label: t.nav_workspaces },
    { href: "/chat", label: t.nav_chat },
    { href: "/settings", label: t.nav_settings },
    ...(isSuperuser
      ? [{ href: "/admin/users", label: t.nav_admin }]
      : []),
  ];

  return (
    <header className="sticky top-0 z-20 border-b border-outline/15 bg-surface/70 backdrop-blur-xl">
      <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
        <Link href="/" className="font-headline text-on-surface text-lg tracking-tight">
          QueryMind <span className="text-primary">AI</span>
        </Link>
        <nav className="flex items-center gap-1">
          {nav.map((n) => {
            const active = pathname === n.href || (n.href !== "/" && pathname.startsWith(n.href));
            return (
              <Link
                key={n.href}
                href={n.href}
                className={
                  "px-3 py-1.5 rounded-xl text-sm transition " +
                  (active
                    ? "bg-primary-container/30 text-primary"
                    : "text-on-surface-variant hover:text-on-surface")
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
              className="px-3 py-1.5 rounded-xl text-sm text-on-surface-variant hover:text-error"
            >
              {t.nav_sign_out}
            </button>
          ) : (
            <Link
              href="/login"
              className="px-3 py-1.5 rounded-xl text-sm bg-primary-container/30 text-primary"
            >
              {t.nav_sign_in}
            </Link>
          )}
        </nav>
      </div>
    </header>
  );
}
