import type { Metadata } from "next";
import { Space_Grotesk, Inter, JetBrains_Mono } from "next/font/google";
import type { ReactNode } from "react";
import { AppHeader } from "@/components/AppHeader";
import { ToastProvider } from "@/components/Toast";
import { I18nProvider } from "@/lib/i18n/context";
import "./globals.css";

const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-space-grotesk",
  display: "swap",
});

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "QueryMind AI",
  description:
    "Natural-language analytics for your own database. Self-hosted, read-only, local-LLM.",
};

/**
 * Phase 43 — Neural Dark v2 background.
 *
 * The design brief is strict: "Flat layered surfaces — no decorative
 * gradients, no imagery, no textures behind content. The one permitted
 * atmosphere is a faint radial accent glow (cyan at ≤6% alpha) and an
 * optional subtle dotted-grid. Never the purple-gradient AI cliché."
 *
 * So the layer below is one single cyan glow at 6% + a 5%-dot grid,
 * over the deepest canvas. Violet is now reserved for chart series.
 */
function GlassBackground() {
  return (
    <div
      aria-hidden
      className="qm-dot-grid pointer-events-none fixed inset-0 -z-10"
      style={{
        backgroundColor: "var(--bg-canvas)",
        backgroundImage: [
          // single cyan atmospheric glow, top-center; ≤6% alpha
          "radial-gradient(ellipse 80% 60% at 50% 0%, rgba(52, 220, 203, 0.06), transparent 70%)",
          // 5% dot-grid texture (unchanged from before)
          "radial-gradient(rgba(234, 240, 248, 0.04) 1px, transparent 1px)",
        ].join(", "),
        backgroundSize: "auto, 24px 24px",
      }}
    />
  );
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      className={`${spaceGrotesk.variable} ${inter.variable} ${jetbrainsMono.variable}`}
    >
      <body className="font-body text-nd-fg-0">
        <GlassBackground />
        <I18nProvider>
          <ToastProvider>
            <AppHeader />
            {children}
          </ToastProvider>
        </I18nProvider>
      </body>
    </html>
  );
}
