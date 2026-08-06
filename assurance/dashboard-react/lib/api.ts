import type { SummaryResponse, LlmEvalsResponse } from "./types";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function fetchSummary(window: number): Promise<SummaryResponse> {
  const r = await fetch(`${BASE_URL}/metrics/summary?window=${window}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`API error ${r.status}`);
  return r.json();
}

export async function fetchLlmEvals(window: number): Promise<LlmEvalsResponse> {
  const r = await fetch(`${BASE_URL}/llm-evals?window=${window}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`API error ${r.status}`);
  return r.json();
}
