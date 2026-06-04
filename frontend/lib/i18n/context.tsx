"use client";

/**
 * Phase 40 — React context for locale + translations.
 *
 * Wrap the app in <I18nProvider>; consumers call `const t = useT()`
 * and access strings via `t.nav_workspaces`. Locale switch updates
 * localStorage and re-renders everything that uses the hook.
 *
 * Hydration note: the server pass always renders with locale="en"
 * (see detectLocale), then the first client effect picks up the
 * stored preference. This means non-English users will briefly see
 * English on first paint; we accept the tradeoff to keep SSR
 * deterministic. A redesign that ships a <html lang=""> attribute
 * can also opt into a cookie-based detector here.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  detectLocale,
  LOCALES,
  LOCALE_LABEL,
  MESSAGES,
  persistLocale,
  type Locale,
  type Messages,
} from "@/lib/i18n/messages";

type I18nValue = {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: Messages;
};

const I18nContext = createContext<I18nValue | null>(null);


export function I18nProvider({ children }: { children: ReactNode }) {
  // SSR-safe initial — always "en" on the server pass.
  const [locale, setLocaleState] = useState<Locale>("en");

  useEffect(() => {
    // Run once on mount: read stored / browser preference.
    setLocaleState(detectLocale());
  }, []);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    persistLocale(l);
  }, []);

  const value = useMemo<I18nValue>(
    () => ({ locale, setLocale, t: MESSAGES[locale] }),
    [locale, setLocale],
  );

  return (
    <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
  );
}


/** Access the bundle. The default export is the messages object —
 *  callers do `const t = useT(); t.nav_workspaces`. */
export function useT(): Messages {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    // Defensive: when a component renders outside the provider
    // (storybook / standalone preview) fall back to English so
    // strings still appear.
    return MESSAGES.en;
  }
  return ctx.t;
}

/** Access locale + setter — used by the language switcher. */
export function useLocale() {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    return {
      locale: "en" as Locale,
      setLocale: (() => {}) as (l: Locale) => void,
    };
  }
  return { locale: ctx.locale, setLocale: ctx.setLocale };
}


export { LOCALES, LOCALE_LABEL };
export type { Locale };
