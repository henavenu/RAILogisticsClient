"use client";

import { useState } from "react";
import { Play, Sparkles, AlertTriangle } from "lucide-react";
import { fetchLlmEvals } from "@/lib/api";
import type { LlmEvalsResponse, MetricDetail, Judgment } from "@/lib/types";
import { Header, type WindowHours } from "@/components/dashboard/Header";
import { DashboardFooter } from "@/components/dashboard/Footer";
import { Card, CardHeader } from "@/components/ui/Card";
import { Badge, statusToTone } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { cn } from "@/lib/cn";
import { isMetricVisible, type AppFilter } from "@/lib/metricApps";

const METRIC_LABELS: Record<string, { label: string; type: "bundled" | "custom"; pillar: string }> = {
  demographic_bias_rate:  { label: "Demographic Bias",  type: "custom",  pillar: "Fairness" },
  decision_fairness_rate: { label: "Decision Fairness", type: "custom",  pillar: "Fairness" },
  refusal_rate:           { label: "Refusal",           type: "bundled", pillar: "Safety" },
  correctness_rate:       { label: "Correctness",       type: "bundled", pillar: "Reliability" },
};

const JUDGMENT_KEY: Record<string, keyof LlmEvalsResponse["judgments"]> = {
  demographic_bias_rate:  "demographic_bias",
  decision_fairness_rate: "decision_fairness",
  refusal_rate:           "refusal",
  correctness_rate:       "correctness",
};

function formatRate(m: MetricDetail) {
  return `${(m.value * 100).toFixed(1)}%`;
}

function MetricCard({
  metricKey,
  metric,
  judgments,
  expanded,
  onToggle,
}: {
  metricKey: string;
  metric: MetricDetail;
  judgments: Judgment[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const meta = METRIC_LABELS[metricKey];
  const valueColor =
    metric.status === "ok" ? "text-green-bright" :
    metric.status === "warning" ? "text-amber" : "text-red";

  return (
    <Card>
      <div className="px-4 py-3 border-b border-border flex items-center gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <Sparkles size={12} className="text-violet" />
            <span className="text-[12px] font-semibold uppercase tracking-[0.12em]">{meta?.label ?? metricKey}</span>
            <Badge tone={meta?.type === "bundled" ? "violet" : "info"}>
              {meta?.type === "bundled" ? "BUNDLED" : "CUSTOM"}
            </Badge>
            <Badge>{meta?.pillar}</Badge>
          </div>
          <div className="text-[10px] text-text-muted mt-0.5 tabular">
            {metric.span_count} spans judged · threshold {metric.threshold ?? "—"}
          </div>
        </div>
        <div className={cn("text-[28px] font-bold tabular", valueColor)}>{formatRate(metric)}</div>
        <Badge tone={statusToTone(metric.status)}>{metric.status}</Badge>
      </div>
      <div className="px-4 py-2 flex flex-wrap gap-2 border-b border-border bg-bg/40">
        {Object.entries(metric.breakdown).map(([k, v]) => (
          <span key={k} className="text-[11px] text-text-muted bg-surface-elevated px-2 py-0.5 rounded tabular">
            {k}: <span className="text-text-secondary font-semibold">{typeof v === "number" ? v : String(v)}</span>
          </span>
        ))}
      </div>

      <button
        onClick={onToggle}
        className="w-full px-4 py-2 text-[11px] text-text-secondary hover:bg-surface-elevated/50 flex items-center justify-between"
      >
        <span>{expanded ? "Hide" : "Show"} {judgments.length} per-span judgments + explanations</span>
        <span className="text-text-muted">{expanded ? "▼" : "▶"}</span>
      </button>

      {expanded && (
        <div className="divide-y divide-border/40">
          {judgments.length === 0 && (
            <div className="px-4 py-6 text-center text-[11px] text-text-muted">
              No spans had the required fields. (Often Arize drops <code className="text-violet">rai.user_message</code> on support_chat spans.)
            </div>
          )}
          {judgments.map((j) => {
            const isFlag = ["biased", "discriminatory", "incorrect"].includes(j.label);
            return (
              <div key={`${metricKey}-${j.span_index}`} className="px-4 py-2.5 hover:bg-surface-elevated/30">
                <div className="flex items-start gap-2">
                  <Badge tone={isFlag ? "critical" : "ok"}>{j.label}</Badge>
                  <span className="text-[10px] text-text-muted">span #{j.span_index}</span>
                </div>
                {j.explanation && (
                  <p className="text-[11px] text-text-secondary mt-1.5 leading-relaxed line-clamp-3">
                    {j.explanation}
                  </p>
                )}
                {j.input && Object.keys(j.input).length > 0 && (
                  <details className="mt-1">
                    <summary className="text-[10px] text-text-muted cursor-pointer hover:text-text-secondary">
                      input
                    </summary>
                    <pre className="text-[10px] text-text-muted bg-bg/60 px-2 py-1 mt-1 rounded overflow-x-auto whitespace-pre-wrap">
                      {JSON.stringify(j.input, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export default function LlmEvalsPage() {
  const [window, setWindow] = useState<WindowHours>(1);
  const [app, setApp] = useState<AppFilter>("all");
  const [data, setData] = useState<LlmEvalsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetchLlmEvals(window);
      setData(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to run LLM evals");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col">
      <Header
        window={window}
        onWindowChange={setWindow}
        onRefresh={run}
        loading={loading}
        asOf={data?.as_of ?? null}
        app={app}
        onAppChange={setApp}
      />

      <main className="flex-1 px-6 py-5 max-w-[1400px] w-full mx-auto space-y-4">
        <div className="flex items-start gap-4">
          <div className="flex-1">
            <h1 className="text-[18px] font-bold tracking-tight flex items-center gap-2">
              <Sparkles size={16} className="text-violet" />
              LLM Eval Deep Dive
            </h1>
            <p className="text-[12px] text-text-secondary mt-1 max-w-2xl">
              Phoenix LLM-as-judge over the live Arize trace window. Custom evaluators (Demographic Bias, Decision Fairness)
              + Phoenix bundled evaluators (Refusal, Correctness). Batch concurrent execution via{" "}
              <code className="text-violet">async_evaluate_dataframe</code>.
            </p>
          </div>
          <button
            onClick={run}
            disabled={loading}
            className="inline-flex items-center gap-2 px-4 py-2 rounded border border-violet/40 bg-violet/10 hover:bg-violet/20 text-violet font-semibold text-[12px] disabled:opacity-50 transition-colors"
          >
            {loading ? <Spinner /> : <Play size={13} />}
            Run LLM Evals ({window}h)
          </button>
        </div>

        {error && (
          <div className="p-3 rounded border border-red/30 bg-red/10 text-[12px] text-red flex items-center gap-2">
            <AlertTriangle size={14} />
            {error}
          </div>
        )}

        {!data && !loading && !error && (
          <div className="border border-dashed border-border rounded-md p-10 text-center">
            <Sparkles size={20} className="text-violet mx-auto mb-2" />
            <div className="text-[13px] text-text-secondary">
              Click <span className="text-violet font-semibold">Run LLM Evals</span> to fetch spans and judge them.
            </div>
            <div className="text-[11px] text-text-muted mt-1">
              ~5–15 seconds depending on span count. Phoenix runs concurrently with auto-retry.
            </div>
          </div>
        )}

        {data && (
          <>
            <Card>
              <CardHeader title="Run Summary" accent="violet" />
              <div className="grid grid-cols-4 gap-px bg-border">
                <div className="bg-surface px-4 py-3">
                  <div className="text-[10px] uppercase tracking-wider text-text-muted">Window</div>
                  <div className="text-[18px] font-bold tabular">{data.window_hours}h</div>
                </div>
                <div className="bg-surface px-4 py-3">
                  <div className="text-[10px] uppercase tracking-wider text-text-muted">Project</div>
                  <div className="text-[18px] font-bold">{data.project}</div>
                </div>
                <div className="bg-surface px-4 py-3">
                  <div className="text-[10px] uppercase tracking-wider text-text-muted">Loan spans</div>
                  <div className="text-[18px] font-bold tabular">{data.loan_span_count}</div>
                </div>
                <div className="bg-surface px-4 py-3">
                  <div className="text-[10px] uppercase tracking-wider text-text-muted">Support spans</div>
                  <div className="text-[18px] font-bold tabular">{data.support_span_count}</div>
                </div>
              </div>
            </Card>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {Object.entries(data.metrics)
                .filter(([key]) => isMetricVisible(key, app))
                .map(([key, metric]) => (
                  <MetricCard
                    key={key}
                    metricKey={key}
                    metric={metric}
                    judgments={data.judgments[JUDGMENT_KEY[key]] ?? []}
                    expanded={expanded === key}
                    onToggle={() => setExpanded(expanded === key ? null : key)}
                  />
                ))}
              {Object.keys(data.metrics).filter((k) => isMetricVisible(k, app)).length === 0 && (
                <div className="col-span-full border border-dashed border-border rounded-md p-8 text-center text-[12px] text-text-muted">
                  No LLM-judge metrics for app filter <span className="text-violet font-semibold">{app}</span>.
                </div>
              )}
            </div>
          </>
        )}
      </main>

      <DashboardFooter />
    </div>
  );
}
