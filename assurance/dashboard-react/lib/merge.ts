import type { LlmEvalsResponse, MetricDetail, PillarSummary, Status, SummaryResponse } from "./types";

const LLM_PILLAR_MAP: Record<string, "fairness" | "safety" | "reliability"> = {
  demographic_bias_rate:  "fairness",
  decision_fairness_rate: "fairness",
  refusal_rate:           "safety",
  correctness_rate:       "reliability",
};

const STATUS_RANK: Record<Status, number> = { ok: 0, warning: 1, critical: 2 };

function worstStatus(a: Status, b: Status): Status {
  return STATUS_RANK[a] >= STATUS_RANK[b] ? a : b;
}

export function mergeLlmIntoSummary(
  summary: SummaryResponse,
  llmEvals: LlmEvalsResponse,
): SummaryResponse {
  const pillars: SummaryResponse["pillars"] = { ...summary.pillars };
  let addedMetrics = 0;
  let addedWarnings = 0;

  for (const [name, metric] of Object.entries(llmEvals.metrics) as [string, MetricDetail][]) {
    const pillarKey = LLM_PILLAR_MAP[name];
    if (!pillarKey) continue;
    const current: PillarSummary | undefined = pillars[pillarKey];
    if (!current) continue;
    if (current.metrics.includes(name)) continue;

    const isWarning = metric.status !== "ok";
    pillars[pillarKey] = {
      ...current,
      metrics: [...current.metrics, name],
      details: { ...current.details, [name]: metric },
      metric_count: current.metric_count + 1,
      warnings: current.warnings + (isWarning ? 1 : 0),
      status: worstStatus(current.status, metric.status),
    };
    addedMetrics += 1;
    if (isWarning) addedWarnings += 1;
  }

  const overall_status = (Object.values(pillars) as PillarSummary[]).reduce<Status>(
    (acc, p) => worstStatus(acc, p.status),
    "ok",
  );

  return {
    ...summary,
    pillars,
    total_metrics: summary.total_metrics + addedMetrics,
    total_warnings: summary.total_warnings + addedWarnings,
    overall_status,
  };
}
