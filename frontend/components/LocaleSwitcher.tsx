"use client";

/**
 * Phase 40 — uz / ru / en switcher pill in the top header.
 *
 * Three buttons (compact for the header bar). Clicking persists
 * the choice to localStorage and re-renders everything through
 * the i18n context.
 */
import {
  LOCALES,
  LOCALE_LABEL,
  useLocale,
  type Locale,
} from "@/lib/i18n/context";
import { cn } from "@/lib/cn";


export function LocaleSwitcher() {
  const { locale, setLocale } = useLocale();
  return (
    <div
      role="group"
      aria-label="Language"
      className={cn(
        "inline-flex items-center gap-0.5 p-0.5 rounded-lg",
        "border border-nd-border-subtle bg-nd-bg-1",
      )}
    >
      {LOCALES.map((l) => (
        <button
          key={l}
          type="button"
          onClick={() => setLocale(l)}
          title={LOCALE_LABEL[l]}
          aria-pressed={locale === l}
          className={cn(
            "px-2 py-0.5 text-xs uppercase tracking-wider rounded-md transition",
            locale === l
              ? "bg-nd-accent/60 text-nd-accent"
              : "text-nd-fg-2 hover:text-nd-fg-0",
          )}
        >
          {short(l)}
        </button>
      ))}
    </div>
  );
}


function short(l: Locale): string {
  switch (l) {
    case "uz":
      return "UZ";
    case "ru":
      return "RU";
    case "en":
      return "EN";
  }
}
