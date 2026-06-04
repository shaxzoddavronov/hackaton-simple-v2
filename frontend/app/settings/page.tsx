"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { GlassPanel } from "@/components/GlassPanel";
import { api, getToken } from "@/lib/api";

type SettingsOut = {
  vllm_endpoint: string;
  vllm_model: string;
};

export default function SettingsPage() {
  const router = useRouter();
  const [data, setData] = useState<SettingsOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api<SettingsOut>("/settings")
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [router]);

  return (
    <main className="mx-auto max-w-2xl px-4 py-8 space-y-4">
      <header>
        <p className="font-mono text-label-caps uppercase text-nd-fg-2">
          Settings
        </p>
        <h1 className="font-headline text-headline-lg text-nd-fg-0 mt-1">
          Inference target
        </h1>
        <p className="text-nd-fg-2 text-sm mt-1">
          QueryMind talks only to a local vLLM server. Change these by
          editing <span className="font-mono">.env</span> and restarting
          the backend.
        </p>
      </header>

      {error ? (
        <GlassPanel className="px-5 py-4 text-nd-error">{error}</GlassPanel>
      ) : !data ? (
        <GlassPanel className="px-5 py-4 text-nd-fg-2">
          Loading…
        </GlassPanel>
      ) : (
        <GlassPanel className="px-5 py-4 space-y-3">
          <Row label="vLLM endpoint" value={data.vllm_endpoint} />
          <Row label="Model" value={data.vllm_model} />
        </GlassPanel>
      )}
    </main>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-nd-border-subtle last:border-0 py-2">
      <span className="text-nd-fg-2 text-sm uppercase tracking-wider">
        {label}
      </span>
      <span className="font-mono text-nd-fg-0">{value}</span>
    </div>
  );
}
