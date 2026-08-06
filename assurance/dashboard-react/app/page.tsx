"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchLlmEvals, fetchSummary } from "@/lib/api";
import { mergeLlmIntoSummary } from "@/lib/merge";
import type { LlmEvalsResponse, SummaryResponse } from "@/lib/types";
import { Header, type WindowHours } from "@/components/dashboard/Header";
import { KpiStrip } from "@/components/dashboard/KpiStrip";
import { PolicyEvalsRail } from "@/components/dashboard/PolicyEvalsRail";
import { CenterPanels } from "@/components/dashboard/CenterPanels";
import { AssuranceRail } from "@/components/dashboard/AssuranceRail";
import { DashboardFooter } from "@/components/dashboard/Footer";
import type { AppFilter } from "@/lib/metricApps";

export default function DashboardPage() {
  const [window, setWindow] = useState<WindowHours>(1);
  const [app, setApp] = useState<AppFilter>("all");
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [llmEvals, setLlmEvals] = useState<LlmEvalsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [llmLoading, setLlmLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (w: WindowHours) => {
    setLoading(true);
    setLlmLoading(true);
    setError(null);
    setLlmEvals(null);

    fetchSummary(w)
      .then(setSummary)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to fetch metrics"))
      .finally(() => setLoading(false));

    fetchLlmEvals(w)
      .then(setLlmEvals)
      .catch(() => { /* surface only fast-path errors; LLM judges are best-effort */ })
      .finally(() => setLlmLoading(false));
  }, []);

  useEffect(() => {
    load(window);
  }, [window, load]);

  const mergedSummary = useMemo(() => {
    if (!summary) return null;
    if (!llmEvals) return summary;
    return mergeLlmIntoSummary(summary, llmEvals);
  }, [summary, llmEvals]);

  return (
    <div className="min-h-screen flex flex-col">
      <Header
        window={window}
        onWindowChange={setWindow}
        onRefresh={() => load(window)}
        loading={loading}
        asOf={summary?.as_of ?? null}
        app={app}
        onAppChange={setApp}
      />
      <KpiStrip />

      <main className="flex-1 px-4 py-3 grid grid-cols-[200px_1fr_320px] gap-3 max-w-[1600px] w-full mx-auto">
        <PolicyEvalsRail />
        <CenterPanels />
        <AssuranceRail
          summary={mergedSummary}
          loading={loading}
          error={error}
          app={app}
          llmLoading={llmLoading}
        />
      </main>

      <DashboardFooter />
    </div>
  );
}
