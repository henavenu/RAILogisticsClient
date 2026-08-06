# RAI Assurance — Pillars, Metrics & Technical Approach

A presentation-ready brief covering the four assurance pillars, the 17 metrics we ship today, how each is computed, and why our LLM-as-judge metrics live in a separate path.

---

## 1. The Four Assurance Pillars

Responsible AI Assurance is the observability layer that monitors AI systems across four dimensions. Each pillar answers a different question:

| Pillar | The question it answers |
|---|---|
| **Fairness** | Does the system treat demographic groups equitably? |
| **Safety** | Is the output free of harmful, toxic, or offensive content? |
| **Reliability** | Does the system perform consistently and respond on time? |
| **Privacy** | Is sensitive data being exposed in traces or responses? |

Together: **17 metrics** — 13 served on the live dashboard (sub-2s), 4 served on the LLM Eval Deep Dive page (on-demand, ~5-15s).

---

## 2. Pillars and Metrics

### 2.1 Fairness — 4 statistical + 2 LLM-judge

| Metric | What it measures |
|---|---|
| `approval_rate_disparity_by_gender` | Spread (max − min) of loan approval rates across gender groups. Higher = more disparate treatment. |
| `denial_rate_disparity_by_gender` | Same logic for denials. |
| `risk_score_disparity_by_gender` | Spread of average risk scores assigned across genders. |
| `approval_rate_disparity_by_age` | Approval rate spread across age cohorts (18–30, 31–45, 46+). |
| `demographic_bias_rate` *(LLM)* | Fraction of support-chat responses an LLM judge flags as showing demographic bias (stereotyping, condescension, disparate effort). |
| `decision_fairness_rate` *(LLM)* | Fraction of loan decisions where the stated rationale invokes a protected attribute instead of legitimate financial factors. |

### 2.2 Safety — 4 statistical + 1 LLM-judge

| Metric | What it measures |
|---|---|
| `toxicity_rate` | % of support-chat spans flagged toxic by an upstream classifier. |
| `offensive_content_rate` | % flagged offensive. |
| `escalation_rate` | % of support interactions that needed human escalation. |
| `negative_sentiment_rate` | % of responses with negative customer sentiment. |
| `refusal_rate` *(LLM)* | % of support-chat responses where the agent refused vs answered. Useful to catch over-refusal (helpfulness drop) or under-refusal (harm leakage). |

### 2.3 Reliability — 4 statistical + 1 LLM-judge

| Metric | What it measures |
|---|---|
| `decisioning_latency_p50_ms` | Median end-to-end latency of the loan decision flow. |
| `conversational_latency_p50_ms` | Median latency of support chat responses. |
| `decisioning_score_consistency` | Standard deviation of risk scores — measures whether the model is producing stable, reproducible scores. |
| `conversational_safe_interaction_rate` | Fraction of support interactions free of safety flags. |
| `correctness_rate` *(LLM)* | Fraction of support responses an LLM judge labels as correctly answering the user's question. |

### 2.4 Privacy — 1 statistical

| Metric | What it measures |
|---|---|
| `pii_detection_rate` | Fraction of spans where any field contains personally identifiable info (email, phone, SSN, credit-card patterns). Cross-cuts both loan and support spans. |

---

## 3. How Each Metric Is Computed (Technical Approach)

We use **four distinct techniques** depending on the metric's nature. Knowing which technique a metric uses tells you about its cost, speed, and confidence profile.

### Approach A — Statistical aggregation
Pure Python math over span attributes. No external API, no model calls. Sub-millisecond per metric.

> *We pull spans from Arize, group them by demographic attribute, and compute disparities, percentiles, or standard deviations.*

| Metric | Calculation |
|---|---|
| `approval_rate_disparity_by_gender` | `max(group_rate) − min(group_rate)` across gender groups |
| `denial_rate_disparity_by_gender` | same |
| `risk_score_disparity_by_gender` | `max(mean_score) − min(mean_score)` across genders |
| `approval_rate_disparity_by_age` | same as gender disparity, age buckets |
| `decisioning_latency_p50_ms` | 50th percentile of loan span durations |
| `conversational_latency_p50_ms` | same for support spans |
| `decisioning_score_consistency` | standard deviation of risk scores across spans |

### Approach B — Upstream classifier flag aggregation
We don't classify ourselves — the LangGraph app emits boolean flags during inference (via `gpt-4o-mini` classifier). We aggregate those pre-existing flags. Cost is amortized into the original inference call.

> *Each span carries `rai.toxicity_flagged`, `rai.offensive_flagged`, etc. We count them.*

| Metric | Calculation |
|---|---|
| `toxicity_rate` | `count(rai.toxicity_flagged=true) / total spans` |
| `offensive_content_rate` | same |
| `escalation_rate` | same |
| `negative_sentiment_rate` | same |
| `conversational_safe_interaction_rate` | `1 − (any-safety-flag rate)` |

### Approach C — Regex pattern matching
Pure pattern detection over span text fields. No model, no third-party. Microseconds per span.

> *We compile four regexes — email, US phone, SSN format, credit-card sequences — and scan every string-typed attribute on every span.*

| Metric | Patterns scanned |
|---|---|
| `pii_detection_rate` | `EMAIL`, `PHONE`, `SSN (XXX-XX-XXXX)`, `CARD (4-group 16 digits)` |

### Approach D — LLM-as-judge (Phoenix evaluators)
A separate LLM (**Claude Haiku 4.5** as the judge, distinct from the model under test) receives a structured prompt with the candidate input/output and emits a labeled judgment. We aggregate the labels across the window. Phoenix's `create_classifier` and bundled evaluators wrap this loop.

| Metric | Type | Evaluator |
|---|---|---|
| `demographic_bias_rate` | **Custom prompt** | Our prompt via `phoenix.evals.create_classifier` |
| `decision_fairness_rate` | **Custom prompt** | Our prompt via `phoenix.evals.create_classifier` |
| `refusal_rate` | **Bundled** | `phoenix.evals.metrics.RefusalEvaluator` (out-of-the-box) |
| `correctness_rate` | **Bundled** | `phoenix.evals.metrics.CorrectnessEvaluator` (out-of-the-box) |

The judge is independent of the model being judged — Claude evaluating GPT-4o-mini's outputs, not self-evaluation.

---

## 4. Why LLM-as-Judge Is Slow

Each judgment involves:
1. Building a prompt that includes the span's input + output
2. An HTTPS call to the Anthropic API
3. The judge model runs full inference (token-by-token generation)
4. Response parsing back into a `label` and `explanation`

**Per-span cost:** ~1–3 seconds round-trip.

**Naïve scaling:**
- 22 spans (typical 1-hour window: 11 loan + 11 support) × 4 evaluators = **88 LLM calls per dashboard refresh**
- Sequential execution: **~90–250 seconds per refresh**
- A live dashboard polling every 30 seconds would cascade thousands of calls per minute

**Compare to the other approaches:**
- Statistical aggregation: ~milliseconds total
- Classifier flag aggregation: ~milliseconds (already computed upstream)
- Regex: ~milliseconds (~22 spans × 4 patterns)
- LLM-as-judge: ~seconds-to-minutes — **3-4 orders of magnitude slower**

This is why running LLM evals on every dashboard tick is a non-starter for cost, latency, and rate limits.

---

## 5. The Workaround We Built

Three layers, in order of impact:

### 5.1 Architectural split — fast path vs cold path

The biggest win. We exposed two distinct API surfaces:

| Endpoint | What it does | When it runs |
|---|---|---|
| `GET /metrics/summary` | The 13 statistical + flag + regex metrics. Sub-2s. | Every dashboard load + window change. |
| `GET /llm-evals` | The 4 LLM-judge metrics with explanations. ~5–15s. | Only when user clicks **Run LLM Evals** on the deep-dive page. |

The live dashboard never triggers an LLM call. The deep-dive page is opt-in.

### 5.2 Batch + concurrency via Phoenix's async runner

When the user *does* trigger LLM evals, we don't loop sequentially. We use Phoenix's `async_evaluate_dataframe` with `concurrency=10`:

```python
out_df = asyncio.run(async_evaluate_dataframe(
    dataframe=df,
    evaluators=[evaluator],
    concurrency=10,
    exit_on_error=False,
))
```

Effect: **22 spans × 4 evaluators in ~5 seconds** instead of ~90+ seconds — a roughly **10× speedup**.

### 5.3 Auto-retry on transient failures

Phoenix's executor retries up to 10 times per failed call by default. We pass `exit_on_error=False` so a single span failure doesn't kill the entire batch. We saw `APIConnectionError` fire during live testing and recover automatically — the user never sees it.

### 5.4 What's next (not yet shipped)

1. **Result caching** — keep `/llm-evals` results in memory or Redis for ~5 minutes so a second click within that window is instant.
2. **Background pre-compute** — schedule a cron that runs LLM evals every N minutes and stores results, so the deep-dive view is instant the moment it opens.
3. **Streaming response** — render judgments incrementally as Phoenix returns them, instead of waiting for the full batch.
4. **Sampling** — for high-volume windows, judge a stratified sample rather than every span.

---

## 6. The One-Line Summary

> RAI Assurance ships **17 metrics across 4 pillars**, computed via four techniques — statistical math, upstream-flag aggregation, regex pattern matching, and LLM-as-judge. The first three run inline on every dashboard refresh in under 2 seconds. The last (LLM-as-judge) is **architecturally split into its own endpoint** so it never blocks the live view; when a user *does* request it, we execute it concurrently via Phoenix's batch runner — typically **5 seconds for ~22 spans across 4 evaluators**.

