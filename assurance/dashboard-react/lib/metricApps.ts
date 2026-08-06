// Maps each metric to the use_case (RAI app) it operates on.
// Used by the App dropdown filter to show/hide metrics per selected app.

export type AppFilter = "all" | "loan_decision" | "support_chat";
export type MetricApp = "loan_decision" | "support_chat" | "both";

export const METRIC_APP: Record<string, MetricApp> = {
  // Fairness — statistical disparities operate on loan_decision spans
  approval_rate_disparity_by_gender: "loan_decision",
  denial_rate_disparity_by_gender:   "loan_decision",
  risk_score_disparity_by_gender:    "loan_decision",
  approval_rate_disparity_by_age:    "loan_decision",

  // Safety — sentiment/toxicity flags come from support_chat
  toxicity_rate:           "support_chat",
  offensive_content_rate:  "support_chat",
  escalation_rate:         "support_chat",
  negative_sentiment_rate: "support_chat",

  // Reliability — split: latency/consistency for loan, latency/safe-interaction for support
  decisioning_latency_p50_ms:           "loan_decision",
  decisioning_score_consistency:        "loan_decision",
  conversational_latency_p50_ms:        "support_chat",
  conversational_safe_interaction_rate: "support_chat",

  // Privacy — scans both
  pii_detection_rate: "both",

  // LLM evals
  demographic_bias_rate:  "support_chat",
  decision_fairness_rate: "loan_decision",
  refusal_rate:           "support_chat",
  correctness_rate:       "support_chat",
};

export function isMetricVisible(metricName: string, app: AppFilter): boolean {
  if (app === "all") return true;
  const m = METRIC_APP[metricName];
  if (!m) return true; // unknown metrics fall through (don't hide)
  return m === "both" || m === app;
}
