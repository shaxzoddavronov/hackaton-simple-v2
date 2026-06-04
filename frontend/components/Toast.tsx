"use client";

/**
 * Hand-rolled toast notification system. One `<ToastProvider>` wraps the
 * app tree (see `app/layout.tsx`), and any client component can call
 * `useToast()` to fire transient messages. No external deps — keeps the
 * frontend bundle lean and the visual language consistent with the rest
 * of the Neural Dark UI (matches `GlassPanel`'s glassmorphism recipe).
 *
 * Constraints:
 *   - Max 4 toasts visible at once (newest slid in at the bottom of the
 *     stack; older ones drop off the front when over budget).
 *   - Auto-dismiss after `ms` (default 5000) via setTimeout, plus
 *     click-to-dismiss.
 *   - The stack itself has `pointer-events: none` so it never blocks
 *     clicks on the page underneath; each toast individually has
 *     `pointer-events: auto` so it can still be dismissed.
 *   - Top-right positioning — the chat sidebar lives on the left, so
 *     this corner is clear across every page.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { cn } from "@/lib/cn";

export type ToastVariant = "success" | "error" | "info";

type ToastItem = {
  id: string;
  message: string;
  variant: ToastVariant;
  ms: number;
};

type ToastApi = {
  toast: (message: string, variant?: ToastVariant, ms?: number) => void;
  success: (message: string, ms?: number) => void;
  error: (message: string, ms?: number) => void;
  info: (message: string, ms?: number) => void;
};

const ToastCtx = createContext<ToastApi | null>(null);

const MAX_VISIBLE = 4;
const DEFAULT_MS = 5000;

function newId(): string {
  if (
    typeof globalThis !== "undefined" &&
    typeof globalThis.crypto?.randomUUID === "function"
  ) {
    return globalThis.crypto.randomUUID();
  }
  return `toast-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  // Track timers so we can clear them if a toast is dismissed by click
  // before its auto-dismiss fires (avoids stale callbacks running after
  // unmount / re-add of the same id-shape).
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: string) => {
    const t = timers.current.get(id);
    if (t) {
      clearTimeout(t);
      timers.current.delete(id);
    }
    setItems((cur) => cur.filter((x) => x.id !== id));
  }, []);

  const toast = useCallback(
    (message: string, variant: ToastVariant = "info", ms: number = DEFAULT_MS) => {
      const id = newId();
      setItems((cur) => {
        const next = [...cur, { id, message, variant, ms }];
        // Cap visible toasts; the oldest ones (front of queue) get
        // dropped. Their timers are no-ops because filter() already
        // removed them — leaving the setTimeout to fire harmlessly.
        return next.length > MAX_VISIBLE
          ? next.slice(next.length - MAX_VISIBLE)
          : next;
      });
      const handle = setTimeout(() => {
        timers.current.delete(id);
        setItems((cur) => cur.filter((x) => x.id !== id));
      }, ms);
      timers.current.set(id, handle);
    },
    [],
  );

  // Clean up any pending timers on provider unmount.
  useEffect(() => {
    const map = timers.current;
    return () => {
      for (const t of map.values()) clearTimeout(t);
      map.clear();
    };
  }, []);

  const api: ToastApi = {
    toast,
    success: (m, ms) => toast(m, "success", ms),
    error: (m, ms) => toast(m, "error", ms),
    info: (m, ms) => toast(m, "info", ms),
  };

  return (
    <ToastCtx.Provider value={api}>
      {children}
      <ToastStack items={items} onDismiss={dismiss} />
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) {
    throw new Error("useToast must be used inside <ToastProvider>");
  }
  return ctx;
}

// ── Visuals ────────────────────────────────────────────────────────

const VARIANT_BORDER: Record<ToastVariant, string> = {
  success: "border-l-4 border-l-tertiary",
  error: "border-l-4 border-l-error",
  info: "border-l-4 border-l-outline",
};

const VARIANT_ICON_COLOR: Record<ToastVariant, string> = {
  success: "text-tertiary",
  error: "text-nd-error",
  info: "text-nd-fg-2",
};

const VARIANT_ICON: Record<ToastVariant, string> = {
  success: "✓",
  error: "✗",
  info: "ⓘ",
};

const VARIANT_LABEL: Record<ToastVariant, string> = {
  success: "Success",
  error: "Error",
  info: "Info",
};

function ToastStack({
  items,
  onDismiss,
}: {
  items: ToastItem[];
  onDismiss: (id: string) => void;
}) {
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      // pointer-events-none on the container lets clicks through to
      // whatever is underneath outside individual toast bounding boxes.
      className="fixed top-4 right-4 z-50 flex flex-col gap-2 max-w-sm pointer-events-none"
    >
      {items.map((t) => (
        <ToastCard key={t.id} item={t} onDismiss={() => onDismiss(t.id)} />
      ))}
    </div>
  );
}

function ToastCard({
  item,
  onDismiss,
}: {
  item: ToastItem;
  onDismiss: () => void;
}) {
  // Slide-in animation: start translated/transparent, swap to neutral
  // after mount on the next frame so the CSS transition runs.
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setShown(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <button
      type="button"
      onClick={onDismiss}
      role="status"
      aria-label={`${VARIANT_LABEL[item.variant]}: ${item.message}. Click to dismiss.`}
      className={cn(
        // Glassmorphism — mirrors GlassPanel but with a tighter radius
        // and a thicker accent border on the left edge.
        "pointer-events-auto",
        "bg-nd-bg-1 backdrop-blur-xl",
        "border border-nd-border-subtle rounded-xl shadow-lg",
        "px-4 py-3 text-left",
        "flex items-start gap-3",
        VARIANT_BORDER[item.variant],
        // Mount transition
        "transition-all duration-200 ease-out",
        shown
          ? "translate-x-0 opacity-100"
          : "translate-x-2 opacity-0",
        // Subtle hover affordance signalling click-to-dismiss
        "hover:bg-nd-bg-1/70",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "font-mono text-base leading-none mt-0.5 shrink-0",
          VARIANT_ICON_COLOR[item.variant],
        )}
      >
        {VARIANT_ICON[item.variant]}
      </span>
      <span className="text-nd-fg-0 text-sm leading-snug break-words flex-1">
        {item.message}
      </span>
    </button>
  );
}

export default ToastProvider;
