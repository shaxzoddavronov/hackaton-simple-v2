import type { Config } from "tailwindcss";

/**
 * Neural Dark design tokens — keep in sync with ui_images/DESIGN.md.
 * Color keys use kebab-case (e.g. `bg-surface-container-high`).
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  // RenderSpec interpolates `col-span-${ch.span}` for dashboard grids, so JIT
  // would otherwise drop them. Keep this list tight — only the 12-col widths.
  safelist: Array.from({ length: 12 }, (_, i) => `col-span-${i + 1}`),
  theme: {
    extend: {
      colors: {
        surface: "#02132d",
        "surface-dim": "#02132d",
        "surface-bright": "#2a3955",
        "surface-container-lowest": "#000e26",
        "surface-container-low": "#0b1b35",
        "surface-container": "#0f1f3a",
        "surface-container-high": "#1b2a45",
        "surface-container-highest": "#263550",
        "on-surface": "#d7e2ff",
        "on-surface-variant": "#bbc9cf",
        "inverse-surface": "#d7e2ff",
        "inverse-on-surface": "#21304c",
        outline: "#859398",
        "outline-variant": "#3c494e",
        "surface-tint": "#3cd7ff",
        primary: "#a8e8ff",
        "on-primary": "#003642",
        "primary-container": "#00d4ff",
        "on-primary-container": "#00586b",
        "inverse-primary": "#00677e",
        secondary: "#d2bbff",
        "on-secondary": "#3f008e",
        "secondary-container": "#6001d1",
        "on-secondary-container": "#c9aeff",
        tertiary: "#00ff88",
        "on-tertiary": "#003919",
        "tertiary-container": "#00df76",
        "on-tertiary-container": "#005d2d",
        error: "#ffb4ab",
        "on-error": "#690005",
        "error-container": "#93000a",
        "on-error-container": "#ffdad6",
        "primary-fixed": "#b4ebff",
        "primary-fixed-dim": "#3cd7ff",
        "on-primary-fixed": "#001f27",
        "on-primary-fixed-variant": "#004e5f",
        "secondary-fixed": "#eaddff",
        "secondary-fixed-dim": "#d2bbff",
        "on-secondary-fixed": "#25005a",
        "on-secondary-fixed-variant": "#5a00c6",
        "tertiary-fixed": "#60ff99",
        "tertiary-fixed-dim": "#00e479",
        "on-tertiary-fixed": "#00210c",
        "on-tertiary-fixed-variant": "#005228",
        background: "#02132d",
        "on-background": "#d7e2ff",
        "surface-variant": "#263550",

        // ── Phase 43 — Claude-design "Neural Dark v2" semantic tokens.
        //    These reference CSS vars set in app/neural-dark.css so a
        //    light-theme override (`<html data-theme="light">`) still
        //    works in print / email. Use these on new / migrated
        //    components; the MD3 keys above stay for back-compat.
        "nd-canvas":  "var(--bg-canvas)",
        "nd-bg-0":    "var(--bg-0)",
        "nd-bg-1":    "var(--bg-1)",
        "nd-bg-2":    "var(--bg-2)",
        "nd-bg-hover":  "var(--bg-hover)",
        "nd-bg-active": "var(--bg-active)",
        "nd-border-subtle": "var(--border-subtle)",
        "nd-border":        "var(--border)",
        "nd-border-strong": "var(--border-strong)",
        "nd-fg-0": "var(--fg-0)",
        "nd-fg-1": "var(--fg-1)",
        "nd-fg-2": "var(--fg-2)",
        "nd-fg-3": "var(--fg-3)",
        "nd-accent":       "var(--accent)",
        "nd-accent-hover": "var(--accent-hover)",
        "nd-accent-press": "var(--accent-press)",
        "nd-accent-wash":  "var(--accent-wash)",
        "nd-on-accent":    "var(--on-accent)",
        "nd-ready":    "var(--status-ready)",
        "nd-activity": "var(--status-activity)",
        "nd-error":    "var(--status-error)",
        "nd-auth":     "var(--status-auth)",
        "nd-neutral":  "var(--status-neutral)",
        // 8-step categorical viz palette (for chart series only).
        "nd-viz-1": "var(--qm-viz-1)",
        "nd-viz-2": "var(--qm-viz-2)",
        "nd-viz-3": "var(--qm-viz-3)",
        "nd-viz-4": "var(--qm-viz-4)",
        "nd-viz-5": "var(--qm-viz-5)",
        "nd-viz-6": "var(--qm-viz-6)",
        "nd-viz-7": "var(--qm-viz-7)",
        "nd-viz-8": "var(--qm-viz-8)",
      },
      fontFamily: {
        headline: ["var(--font-space-grotesk)", "Space Grotesk", "sans-serif"],
        body: ["var(--font-inter)", "Inter", "sans-serif"],
        mono: ["var(--font-jetbrains-mono)", "JetBrains Mono", "monospace"],
      },
      fontSize: {
        // DESIGN.md typography tokens — [fontSize, { lineHeight, letterSpacing, fontWeight }]
        "headline-xl": [
          "48px",
          {
            lineHeight: "1.1",
            letterSpacing: "-0.02em",
            fontWeight: "700",
          },
        ],
        "headline-lg": [
          "32px",
          {
            lineHeight: "1.2",
            letterSpacing: "-0.01em",
            fontWeight: "600",
          },
        ],
        "headline-lg-mobile": [
          "24px",
          {
            lineHeight: "1.2",
            fontWeight: "600",
          },
        ],
        "body-md": [
          "16px",
          {
            lineHeight: "1.6",
            fontWeight: "400",
          },
        ],
        "body-sm": [
          "14px",
          {
            lineHeight: "1.5",
            fontWeight: "400",
          },
        ],
        "data-mono": [
          "14px",
          {
            lineHeight: "1.4",
            letterSpacing: "0.02em",
            fontWeight: "500",
          },
        ],
        "label-caps": [
          "12px",
          {
            lineHeight: "1.2",
            letterSpacing: "0.1em",
            fontWeight: "700",
          },
        ],
      },
      borderRadius: {
        // DESIGN.md `rounded` scale
        sm: "0.125rem",
        DEFAULT: "0.25rem",
        md: "0.375rem",
        lg: "0.5rem",
        xl: "0.75rem",
        "2xl": "1rem",
        full: "9999px",
        // Phase 43 — Neural Dark v2 token radii (in pixels, distinct
        // from the rem scale above).
        "nd-sm": "6px",
        "nd-md": "10px",
        "nd-lg": "14px",
        "nd-xl": "20px",
      },
      spacing: {
        // DESIGN.md spacing tokens, mapped to Tailwind-friendly names.
        base: "8px",
        "container-margin": "24px",
        gutter: "16px",
        "stack-sm": "4px",
        "stack-md": "12px",
        "stack-lg": "24px",
      },
      boxShadow: {
        // Cyan luminous glow per DESIGN.md §Elevation & Depth
        glow: "0 0 15px rgba(0, 212, 255, 0.3)",
        // Phase 43 — Neural Dark v2 elevation set.
        "nd-1": "0 1px 2px rgba(0,0,0,.45)",
        "nd-2": "0 6px 20px rgba(0,0,0,.45)",
        "nd-3": "0 20px 56px rgba(0,0,0,.55)",
        "nd-accent":
          "0 0 0 1px rgba(52,220,203,.35), 0 0 24px rgba(52,220,203,.18)",
        "nd-focus": "0 0 0 3px rgba(52,220,203,.30)",
      },
    },
  },
  plugins: [],
};

export default config;
