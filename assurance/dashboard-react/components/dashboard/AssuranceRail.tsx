"use client";

import { useState } from "react";
import Link from "next/link";
import {
  Scale, Shield, Cog, Lock, ChevronRight, Sparkles, AlertTriangle, CheckCircle2,
} from "lucide-react";
import {
  RadarChart, PolarGrid, PolarAngleAxis, Radar, ResponsiveContainer,
} from "recharts";
import type { SummaryResponse, PillarSummary, MetricDetail, Status } from "@/lib/types";
import { cn } from "@/lib/cn";
import { Badge, statusToTone } from "@/components/ui/Badge";
import { isMetricVisible, type AppFilter } from "@/lib/metricApps";

const PILLAR_META = {
  fairness:    { label: "Fairness",    icon: Scale, accent: "violet" as const },
  safety:      { label: "Safety",      icon: Shield, accent: "violet" as const },
  reliability: { label: "Reliability", icon: Cog,   accent: "violet" as const },
  privacy:     { label: "Privacy",     icon: Lock,  accent: "violet" as const },
};

const PILLAR_KEYS = ["fairness", "safety", "reliability", "privacy"] as const;
type PillarKey = typeof PILLAR_KEYS[number];

function formatValue(m: MetricDetail) {
  if (m.unit === "fraction" || m.unit === "disparity") return `${(m.value * 100).toFixed(1)}%`;
  if (m.unit === "ms") return `${m.value.toFixed(0)}ms`;
  if (m.unit === "points") return `${m.value.toFixed(1)}`;
  return m.value.toFixed(2);
}

function filteredPillarView(data: PillarSummary, app: AppFilter) {
  const visibleMetrics = data.metrics.filter((n) => isMetricVisible(n, app));
  const filteredStatuses = visibleMetrics
    .map((n) => data.details[n]?.status)
    .filter(Boolean) as Status[];
  const status: Status =
    filteredStatuses.length === 0       ? "ok" :  // no metrics → not "critical", just empty
    filteredStatuses.includes("critical") ? "critical" :
    filteredStatuses.includes("warning")  ? "warning" : "ok";
  const warnings = filteredStatuses.filter((s) => s !== "ok").length;
  return { visibleMetrics, status, warnings, hiddenCount: data.metrics.length - visibleMetrics.length };
}

function PillarCard({
  pillar,
  data,
  expanded,
  onToggle,
  app,
}: {
  pillar: PillarKey;
  data: PillarSummary;
  expanded: boolean;
  onToggle: () => void;
  app: AppFilter;
}) {
  const meta = PILLAR_META[pillar];
  const Icon = meta.icon;
  const { visibleMetrics, status: filteredWorst, warnings: filteredWarnings, hiddenCount } =
    filteredPillarView(data, app);

  const statusColor =
    filteredWorst === "ok"      ? "border-l-green" :
    filteredWorst === "warning" ? "border-l-amber" :
                                  "border-l-red";
  const top = visibleMetrics
    .map((n) => data.details[n])
    .filter(Boolean)
    .sort((a, b) => (b.status === "ok" ? 0 : 1) - (a.status === "ok" ? 0 : 1))[0];

  return (
    <div className={cn(
      "bg-surface-elevated/40 border border-border rounded-md border-l-2 overflow-hidden transition-all",
      statusColor,
    )}>
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2.5 px-3 py-2.5 hover:bg-surface-elevated/80"
      >
        <Icon size={14} className="text-violet" />
        <span className="text-[12px] font-semibold flex-1 text-left">{meta.label}</span>
        {visibleMetrics.length === 0 ? (
          <Badge>n/a</Badge>
        ) : filteredWarnings > 0 ? (
          <Badge tone="warning">{filteredWarnings} warn</Badge>
        ) : (
          <Badge tone="ok">OK</Badge>
        )}
        <ChevronRight size={14} className={cn("text-text-muted transition-transform", expanded && "rotate-90")} />
      </button>

      {visibleMetrics.length === 0 && (
        <div className="px-3 pb-2.5 text-[10.5px] text-text-muted italic">
          No metrics for the selected app filter.
        </div>
      )}

      {top && !expanded && visibleMetrics.length > 0 && (
        <div className="px-3 pb-2.5 text-[11px] flex items-center gap-2">
          <span className="text-text-muted truncate flex-1">{top.metric_name}</span>
          <span className={cn(
            "tabular font-semibold",
            top.status === "ok" ? "text-green-bright" :
            top.status === "warning" ? "text-amber" : "text-red",
          )}>
            {formatValue(top)}
          </span>
        </div>
      )}

      {expanded && visibleMetrics.length > 0 && (
        <div className="border-t border-border/60 divide-y divide-border/40">
          {hiddenCount > 0 && (
            <div className="px-3 py-1.5 text-[10px] text-text-muted italic bg-bg/40">
              {hiddenCount} metric{hiddenCount === 1 ? "" : "s"} hidden by app filter
            </div>
          )}
          {visibleMetrics.map((name) => {
            const m = data.details[name];
            if (!m) {
              return (
                <div key={name} className="px-3 py-2 text-[11px] flex justify-between">
                  <span className="text-text-muted truncate">{name}</span>
                  <span className="text-text-muted">no data</span>
                </div>
              );
            }
            const color =
              m.status === "ok"      ? "text-green-bright" :
              m.status === "warning" ? "text-amber" : "text-red";
            return (
              <div key={name} className="px-3 py-2 text-[11px]">
                <div className="flex items-center gap-2">
                  <span className="flex-1 text-text-secondary truncate">{name}</span>
                  <span className={cn("tabular font-semibold", color)}>{formatValue(m)}</span>
                </div>
                {m.threshold !== null && m.threshold !== undefined && (
                  <div className="text-[10px] text-text-muted mt-0.5 tabular">
                    threshold {m.threshold} · {m.span_count} spans
                  </div>
                )}
                {Object.keys(m.breakdown).length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {Object.entries(m.breakdown).slice(0, 4).map(([k, v]) => (
                      <span key={k} className="text-[9.5px] text-text-muted bg-bg/40 px-1.5 py-0.5 rounded tabular">
                        {k}: <span className="text-text-secondary">{typeof v === "number" ? v.toFixed(2) : v}</span>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function AssuranceRadar({ summary, app }: { summary: SummaryResponse; app: AppFilter }) {
  // Map pillar status to a 0-100 score (ok=100, warning=50, critical=20).
  // Status is recomputed per pillar from the metrics visible under the App filter,
  // so the radar shape tracks the dropdown selection.
  const score = (s: Status) => (s === "ok" ? 100 : s === "warning" ? 50 : 20);
  const data = PILLAR_KEYS.map((k) => {
    const { status, visibleMetrics } = filteredPillarView(summary.pillars[k], app);
    // No visible metrics for this pillar under the filter → score 0 so the radar caves in there.
    return {
      pillar: PILLAR_META[k].label,
      score: visibleMetrics.length === 0 ? 0 : score(status),
    };
  });

  return (
    <div className="px-3 py-3 border-b border-border bg-gradient-to-b from-violet/5 to-transparent">
      <div className="flex items-center gap-1.5 mb-1">
        <Sparkles size={11} className="text-violet" />
        <span className="text-[10px] font-semibold tracking-[0.15em] uppercase text-violet">
          Assurance Health · {summary.window_hours}h
        </span>
      </div>
      <div className="h-[150px]">
        <ResponsiveContainer>
          <RadarChart data={data} outerRadius={55}>
            <PolarGrid stroke="var(--border)" />
            <PolarAngleAxis
              dataKey="pillar"
              tick={{ fill: "var(--text-secondary)", fontSize: 10 }}
            />
            <Radar
              dataKey="score"
              stroke="var(--violet)"
              fill="var(--violet)"
              fillOpacity={0.25}
              isAnimationActive={false}
            />
          </RadarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function AssuranceRail({
  summary,
  loading,
  error,
  app,
  llmLoading = false,
}: {
  summary: SummaryResponse | null;
  loading: boolean;
  error: string | null;
  app: AppFilter;
  llmLoading?: boolean;
}) {
  const [openPillar, setOpenPillar] = useState<PillarKey | null>("fairness");

  return (
    <div className="bg-surface border border-violet/30 rounded-md overflow-hidden shadow-[0_0_30px_rgba(167,139,250,0.06)]">
      <div className="px-3 py-2.5 border-b border-border bg-gradient-to-r from-violet/15 via-violet/5 to-transparent flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Sparkles size={13} className="text-violet" />
          <div>
            <div className="text-[11px] font-bold tracking-[0.15em] uppercase text-violet">
              RAI Assurance Layer
            </div>
            <div className="text-[9.5px] text-text-muted tracking-wider">Tier 2</div>
          </div>
        </div>
        {summary && (
          <Badge tone={statusToTone(summary.overall_status)} pulse={summary.overall_status !== "ok"}>
            {summary.overall_status}
          </Badge>
        )}
      </div>

      {error && (
        <div className="m-3 p-3 rounded border border-red/30 bg-red/10 text-[11px] text-red">
          <AlertTriangle size={12} className="inline mr-1" />
          {error}
        </div>
      )}

      {summary && <AssuranceRadar summary={summary} app={app} />}

      {summary && (
        <div className="px-3 py-2 grid grid-cols-2 gap-2 border-b border-border bg-bg/40">
          <div className="text-[10px] flex items-center gap-1">
            <CheckCircle2 size={11} className="text-green" />
            <span className="text-text-muted">Metrics</span>
            <span className="ml-auto tabular text-text-primary font-semibold">{summary.total_metrics}</span>
          </div>
          <div className="text-[10px] flex items-center gap-1">
            <AlertTriangle size={11} className="text-amber" />
            <span className="text-text-muted">Warnings</span>
            <span className="ml-auto tabular text-amber font-semibold">{summary.total_warnings}</span>
          </div>
        </div>
      )}

      {llmLoading && (
        <div className="px-3 py-1.5 border-b border-border/60 bg-violet/5 text-[10px] text-violet flex items-center gap-1.5">
          <Sparkles size={10} className="animate-pulse" />
          Phoenix LLM judges running…
        </div>
      )}

      <div className="p-2.5 space-y-2">
        {loading && !summary && (
          <div className="py-8 text-center text-[11px] text-text-muted">Loading...</div>
        )}
        {summary && PILLAR_KEYS.map((k) => (
          <PillarCard
            key={k}
            pillar={k}
            data={summary.pillars[k]}
            expanded={openPillar === k}
            onToggle={() => setOpenPillar(openPillar === k ? null : k)}
            app={app}
          />
        ))}
      </div>

      <Link
        href="/llm-evals"
        className="block px-3 py-2.5 border-t border-border bg-violet/5 hover:bg-violet/10 transition-colors"
      >
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-violet font-semibold tracking-wide uppercase">LLM Eval Deep Dive</span>
          <ChevronRight size={13} className="text-violet" />
        </div>
        <div className="text-[10px] text-text-muted mt-0.5">
          Phoenix LLM-as-judge · custom + bundled evals · per-span explanations
        </div>
      </Link>
    </div>
  );
}
