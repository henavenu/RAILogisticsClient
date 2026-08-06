export type Status = "ok" | "warning" | "critical";

export interface MetricDetail {
  metric_name: string;
  value: number;
  unit: string;
  status: Status;
  span_count: number;
  breakdown: Record<string, number>;
  threshold: number | null;
}

export interface PillarSummary {
  status: Status;
  metric_count: number;
  warnings: number;
  metrics: string[];
  details: Record<string, MetricDetail>;
}

export interface SummaryResponse {
  overall_status: Status;
  pillars: Record<"fairness" | "safety" | "reliability" | "privacy", PillarSummary>;
  total_metrics: number;
  total_warnings: number;
  window_hours: number;
  as_of: string;
}

export interface Judgment {
  span_index: number;
  label: string;
  input: Record<string, unknown>;
  explanation?: string | null;
}

export interface LlmEvalsResponse {
  window_hours: number;
  project: string;
  support_span_count: number;
  loan_span_count: number;
  metrics: Record<
    "demographic_bias_rate" | "decision_fairness_rate" | "refusal_rate" | "correctness_rate",
    MetricDetail
  >;
  judgments: {
    demographic_bias: Judgment[];
    decision_fairness: Judgment[];
    refusal: Judgment[];
    correctness: Judgment[];
  };
  as_of: string;
}
