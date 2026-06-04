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
 * Fixed-position background layer painting the Neural Dark radial blend
 * (surface-container-lowest → surface) plus the 5% dot-grid texture from
 * DESIGN.md §Colors / Surface Strategy. Sits behind all content.
 */
function GlassBackground() {
  return (
    <div
      aria-hidden
      className="qm-dot-grid pointer-events-none fixed inset-0 -z-10"
      style={{
        backgroundColor: "var(--qm-surface)",
        backgroundImage: [
          // primary cyan glow, upper-left
          "radial-gradient(ellipse 60% 50% at 20% 15%, rgba(0, 212, 255, 0.18), transparent 60%)",
          // violet glow, lower-right
          "radial-gradient(ellipse 50% 45% at 85% 90%, rgba(96, 1, 209, 0.22), transparent 65%)",
          // depth blend lowest → surface
          "radial-gradient(ellipse 90% 80% at 50% 50%, #000e26 0%, #02132d 70%)",
          // re-apply dot-grid via inline so the radial blend doesn't paint over it
          "radial-gradient(rgba(215, 226, 255, 0.05) 1px, transparent 1px)",
        ].join(", "),
        backgroundSize: "auto, auto, auto, 24px 24px",
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
      <body className="font-body text-on-surface">
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
