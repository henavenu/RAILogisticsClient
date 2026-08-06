# RAI Assurance Service — Data Flow Deep Dive
**For:** Enterprise Architecture Review  
**Prepared by:** Hena Venugopal  
**Date:** 1 May 2026  

---

## The Central Question

> *"The Assurance API should not pull data directly from the metrics DB — it should go through the Provider SPI."*

This document explains exactly how data flows through the system today, where metrics are computed, what role Arize plays, and what the enterprise architect's comment means for the architecture.

---

## Part 1 — End-to-End Data Flow (Current State)

There are **two distinct data pipelines** running in the system. Understanding both is key.

---

### Pipeline A: AI Decision → Arize (Observability)

```
┌─────────────────────────────────────────────────────────┐
│  LangGraph AI App  (loan_decision.py)                   │
│                                                         │
│  Applicant arrives:                                     │
│    age=34, gender="female", income=72000,               │
│    credit_score=680, loan_amount=25000                  │
│                                                         │
│  Node 1: assess_risk  ──► GPT-4o-mini                   │
│    → risk_level="low", risk_score=32                    │
│                                                         │
│  Node 2: make_decision ──► GPT-4o-mini                  │
│    → decision="approved"                                │
│                                                         │
│  OTel Span created with RAI attributes:                 │
│    rai.applicant.gender  = "female"                     │
│    rai.applicant.age     = 34                           │
│    rai.applicant.income  = 72000                        │
│    rai.risk_score        = 32                           │
│    rai.decision          = "approved"                   │
│    start_time / end_time (nanoseconds)                  │
└────────────────────────────┬────────────────────────────┘
                             │
              OTel OTLP exporter (tracing.py)
                             │
                  ┌──────────▼──────────┐
                  │   Arize AX (Cloud)  │
                  │                     │
                  │  Stores spans for:  │
                  │  • UI trace viewer  │
                  │  • Latency charts   │
                  │  • Error monitoring │
                  │  • Replay / debug   │
                  └─────────────────────┘
```

**What Arize does:** Arize is an AI observability platform. It receives the raw OTel spans, stores them, and lets you visualise traces in its UI — who called what, how long it took, what attributes were set. Think of it like a flight recorder for your AI agent.

**What Arize does NOT do** (in our system): Arize does NOT compute our 12 RAI metrics. It can offer its own built-in evaluations (toxicity scoring, hallucination detection) but we have not activated those. All metric computation happens in our own Python code.

---

### Pipeline B: Spans → Metrics → API → Dashboard (Assurance)

```
LangGraph AI App
        │
        │  write_span_record()   ← separate from OTel export
        │  (local_provider.py)
        ▼
data/spans.jsonl  ← one JSON line per AI decision
  {"name": "loan_decision",
   "start_time": 1746..., "end_time": 1746...,
   "attributes": {
     "rai.applicant.gender": "female",
     "rai.risk_score": 32,
     "rai.decision": "approved", ...}}
        │
        │  fetch_spans("loan_decision")
        ▼
┌────────────────────────────┐
│   Provider SPI             │  ← AssuranceProvider ABC
│   (LocalProvider)          │     fetch_spans(project, use_case)
└────────────────────────────┘
        │
        │  returns list[dict] — raw span records
        ▼
┌────────────────────────────┐
│   Tier-2 Jobs              │  ← THIS is where metrics are computed
│   (tier2.py)               │     Python math on span attributes
│                            │
│   run_cycle() every 15 min │
└────────────────────────────┘
        │
        │  store.insert(MetricResult)
        ▼
data/metrics.db  (SQLite)
  table: metric_results
  [metric_name, value, status, threshold, breakdown, timestamp]
        │
        │  ← ARCHITECTURE GAP HERE (see Part 3)
        │  store.latest_all()  ← API reads SQLite directly
        ▼
┌────────────────────────────┐
│   Assurance Service API    │
│   FastAPI  :8000           │
│   GET /metrics             │
│   GET /metrics/summary     │
└────────────────────────────┘
        │
        ▼
Streamlit Dashboard :8501   /   daily_report.py
```

---

## Part 2 — How Each Metric Is Actually Computed

Metrics are **not** computed in Arize. They are computed in `assurance/jobs/tier2.py` using pure Python — statistical functions applied to the raw span attribute data fetched by the Provider SPI.

Here is exactly how three representative metrics work:

---

### Fairness Metric: `approval_rate_disparity_by_gender`

**What it measures:** Is the AI approving loans at different rates for men vs women? A disparity above 10% triggers a warning.

**Input:** All `loan_decision` spans from the last 24 hours  
**Relevant span attributes:** `rai.applicant.gender`, `rai.decision`

**Computation (actual code in tier2.py:50–65):**

```python
# Step 1 — group spans by gender, count approvals
by = {}
for span_attributes in spans:
    gender = span_attributes.get("rai.applicant.gender", "unknown")
    bucket = by.setdefault(gender, {"total": 0, "approved": 0})
    bucket["total"] += 1
    if span_attributes.get("rai.decision") == "approved":
        bucket["approved"] += 1

# Step 2 — compute approval rate per gender
# Example result from simulation:
#   male:   18 approved out of 24  → 0.75
#   female: 14 approved out of 20  → 0.70

rates = {"male": 0.75, "female": 0.70}

# Step 3 — disparity = max rate - min rate
disparity = 0.75 - 0.70 = 0.05   → status = "ok"  (threshold: 0.10)

# If disparity = 0.25:            → status = "warning" ⚠
```

**MetricResult stored to SQLite:**
```json
{
  "metric_name": "approval_rate_disparity_by_gender",
  "value": 0.05,
  "unit": "disparity",
  "status": "ok",
  "threshold": 0.10,
  "breakdown": {"male": 0.75, "female": 0.70},
  "span_count": 44,
  "timestamp": "2026-05-01T09:30:00Z"
}
```

---

### Safety Metric: `toxicity_rate`

**What it measures:** What fraction of support chat interactions were flagged as toxic? Above 5% is a warning.

**Input:** All `support_chat` spans from the last 24 hours  
**Relevant span attribute:** `rai.toxicity_flagged` (boolean)

**Computation (tier2.py:131–141):**

```python
flagged = sum(1 for attrs in spans if attrs.get("rai.toxicity_flagged"))
rate = flagged / len(spans)

# Example: 2 toxic out of 40 interactions
rate = 2 / 40 = 0.05  → status = "warning" ⚠  (threshold: 0.05, boundary)
```

**MetricResult:**
```json
{
  "metric_name": "toxicity_rate",
  "value": 0.050,
  "status": "warning",
  "threshold": 0.05,
  "breakdown": {"flagged": 2, "clean": 38},
  "span_count": 40
}
```

---

### Reliability Metric: `loan_decision_latency_p50_ms`

**What it measures:** The median time (p50) for the AI to make a loan decision. Above 5,000 ms is a warning.

**Input:** All `loan_decision` spans  
**Relevant span attributes:** `start_time`, `end_time` (in nanoseconds)

**Computation (tier2.py:189–199):**

```python
latencies_ms = [
    (span["end_time"] - span["start_time"]) / 1_000_000
    for span in spans
]
# Example latencies: [1200, 1450, 1380, 1600, 1250, ...]

p50 = statistics.median(latencies_ms)  # = 1380 ms  → status = "ok"
```

**MetricResult:**
```json
{
  "metric_name": "loan_decision_latency_p50_ms",
  "value": 1380.0,
  "unit": "ms",
  "status": "ok",
  "threshold": 5000.0,
  "breakdown": {"p50": 1380.0, "min": 1200.0, "max": 2100.0}
}
```

---

### Full 12-Metric Map

| Pillar | Metric | Span Source | Key Attributes | Threshold |
|--------|--------|-------------|----------------|-----------|
| **Fairness** | approval_rate_disparity_by_gender | loan_decision | rai.applicant.gender, rai.decision | disparity > 0.10 |
| **Fairness** | denial_rate_disparity_by_gender | loan_decision | rai.applicant.gender, rai.decision | disparity > 0.10 |
| **Fairness** | risk_score_disparity_by_gender | loan_decision | rai.applicant.gender, rai.risk_score | disparity > 10 pts |
| **Fairness** | approval_rate_disparity_by_age | loan_decision | rai.applicant.age, rai.decision | disparity > 0.10 |
| **Safety** | toxicity_rate | support_chat | rai.toxicity_flagged | rate > 0.05 |
| **Safety** | offensive_content_rate | support_chat | rai.safety_category | rate > 0.05 |
| **Safety** | escalation_rate | support_chat | rai.intent | rate > 0.10 |
| **Safety** | negative_sentiment_rate | support_chat | rai.sentiment | rate > 0.50 |
| **Reliability** | loan_decision_latency_p50_ms | loan_decision | start_time, end_time | p50 > 5000 ms |
| **Reliability** | support_response_latency_p50_ms | support_chat | start_time, end_time | p50 > 3000 ms |
| **Reliability** | loan_risk_score_consistency | loan_decision | rai.risk_score | stdev > 20 |
| **Reliability** | support_safe_interaction_rate | support_chat | rai.safety_category, rai.toxicity_flagged | rate < 0.90 |

---

## Part 3 — The Architecture Gap: What the Enterprise Architect Flagged

### Current State (As Built)

The Assurance API currently **bypasses the Provider SPI** and reads the SQLite metrics store directly:

```
Assurance API (main.py)
    │
    │  from assurance import store       ← imports store module directly
    │  store.latest_all()               ← reads SQLite directly
    │  store.latest(name)               ← reads SQLite directly
    ▼
data/metrics.db  (SQLite)
```

This means the API is **tightly coupled** to the SQLite implementation. If you later want to:
- Store metrics in PostgreSQL instead of SQLite
- Serve metrics from a Redis cache for speed
- Pull metrics from a cloud metrics service (Datadog, Azure Monitor)

...you would have to change the API code. That violates the SPI abstraction principle.

---

### Target State (Enterprise Architect's Recommendation)

The API should speak only to the SPI interface — never to a concrete storage layer:

```
Assurance API (main.py)
    │
    │  provider.fetch_metrics()         ← speaks to SPI interface only
    ▼
┌─────────────────────────────────────────┐
│  AssuranceProvider SPI                  │
│  (extended with fetch_metrics method)   │
└──────────┬──────────────────────────────┘
           │
     ┌─────┴──────┬──────────────┐
     ▼            ▼              ▼
LocalProvider  ArizeProvider  MockProvider
(SQLite today) (cloud query)  (test data)
```

**What changes:**

1. **Extend the SPI** — add `fetch_metrics()` to `AssuranceProvider` ABC alongside `fetch_spans()`
2. **LocalProvider** — implement `fetch_metrics()` to read from SQLite (same logic as `store.latest_all()`, just moved behind the interface)
3. **API routes** — replace `store.latest_all()` with `provider.fetch_metrics()`
4. **ArizeProvider** — can implement `fetch_metrics()` to query Arize's evaluation results directly (when activated)

**The benefit:** The API layer is completely decoupled from storage. Swapping SQLite for PostgreSQL, or pulling live metric evaluations directly from Arize, is a one-file change to the provider — zero changes to the API.

---

### Side-by-Side Comparison

| Concern | Current (Direct Store) | Target (Via SPI) |
|---------|----------------------|-----------------|
| API knows storage type | Yes — imports `store` | No — calls interface only |
| Swap SQLite → Postgres | Change API + store | Change only LocalProvider |
| Add Arize live metrics | Change API routes | Add ArizeProvider.fetch_metrics() |
| Add Redis cache | Change API routes | Add RedisProvider |
| Testability | Must mock SQLite module | Inject MockProvider |
| Alignment with SRS | Partial | Full SPI contract compliance |

---

## Part 4 — Complete Architecture Diagram (Target State)

```
╔══════════════════════════════════════════════════════════════════════╗
║  LAYER 1 — AI APPLICATIONS                                          ║
║                                                                      ║
║  ┌──────────────────────┐    ┌──────────────────────┐               ║
║  │  Loan Decision App   │    │  Support Chat App    │               ║
║  │  (LangGraph / GPT)   │    │  (LangGraph / GPT)   │               ║
║  │                      │    │                      │               ║
║  │  OTel span emitted:  │    │  OTel span emitted:  │               ║
║  │  rai.applicant.*     │    │  rai.intent          │               ║
║  │  rai.risk_score      │    │  rai.sentiment       │               ║
║  │  rai.decision        │    │  rai.toxicity_flagged│               ║
║  └──────────┬───────────┘    └──────────┬───────────┘               ║
╚═════════════╪══════════════════════════╪═════════════════════════════╝
              │ OTel OTLP export         │ write_span_record()
              ▼                          ▼
      ┌───────────────┐         ┌─────────────────┐
      │  Arize AX     │         │  data/spans.jsonl│
      │  (Cloud UI,   │         │  (local sink for │
      │   trace view, │         │   metric jobs)   │
      │   debugging)  │         └────────┬────────┘
      └───────────────┘                  │
                                         │
╔════════════════════════════════════════╪═════════════════════════════╗
║  LAYER 2 — PROVIDER SPI                │                             ║
║                                        ▼                             ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │  AssuranceProvider  (ABC interface)                         │    ║
║  │  fetch_spans(project, use_case) → list[dict]                │    ║
║  │  fetch_metrics(project) → list[MetricResult]  ← ADD THIS   │    ║
║  └──────────────┬──────────────┬────────────────┬─────────────┘    ║
║                 │              │                │                    ║
║         LocalProvider   ArizeProvider      MockProvider             ║
║         (spans.jsonl    (Arize API,        (in-memory,              ║
║          + metrics.db)   stub today)        for tests)              ║
╚═════════════════╪══════════════╪════════════════╪════════════════════╝
                  │              │                │
╔═════════════════╪══════════════╪════════════════╪════════════════════╗
║  LAYER 3 — METRIC COMPUTATION                                        ║
║                                                                      ║
║  Tier-2 Jobs (tier2.py)  — APScheduler, every 15 minutes            ║
║                                                                      ║
║  run_cycle():                                                        ║
║    loan_spans = provider.fetch_spans("loan_decision")                ║
║    support_spans = provider.fetch_spans("support_chat")              ║
║                                                                      ║
║    For each metric function:                                         ║
║      result = approval_rate_disparity_by_gender(loan_spans)          ║
║      result = toxicity_rate(support_spans)                           ║
║      result = loan_decision_latency_p50_ms(loan_spans)               ║
║      ... (12 total)                                                  ║
║                                                                      ║
║    → MetricResult(name, value, status, threshold, breakdown)         ║
║    → provider.store_metric(result)  ← or store.insert() today        ║
╚══════════════════════════════════════╤═══════════════════════════════╝
                                       │
                               data/metrics.db
                               (SQLite store)
                                       │
╔══════════════════════════════════════╪═══════════════════════════════╗
║  LAYER 4 — ASSURANCE API (TARGET STATE)                              ║
║                                      │                               ║
║  FastAPI  localhost:8000             │                               ║
║                                      │ provider.fetch_metrics()      ║
║  GET /metrics          ─────────────►│  (NOT store.latest_all())     ║
║  GET /metrics/summary  ─────────────►│                               ║
║  GET /metrics/{name}   ─────────────►│                               ║
║  POST /jobs/run        ─────────────►│ triggers run_cycle()          ║
╚══════════════════════════════════════╪═══════════════════════════════╝
                                       │
╔══════════════════════════════════════╪═══════════════════════════════╗
║  LAYER 5 — CONSUMERS                 │                               ║
║                                      ▼                               ║
║  ┌───────────────────┐  ┌───────────────────┐  ┌─────────────────┐  ║
║  │ Streamlit         │  │  daily_report.py  │  │  CI/CD gate     │  ║
║  │ Dashboard :8501   │  │  (CLI / email)    │  │  (future)       │  ║
║  └───────────────────┘  └───────────────────┘  └─────────────────┘  ║
╚══════════════════════════════════════════════════════════════════════╝
```

---

## Part 5 — Where Arize Fits vs Where We Compute

| Question | Answer |
|----------|--------|
| Are our 12 RAI metrics computed in Arize? | **No.** Computed in our Python code in `tier2.py` |
| What does Arize compute? | Arize stores and visualises raw OTel spans. It can run its own built-in LLM evaluations (hallucination, toxicity scoring), but we have not activated those |
| Why send spans to Arize at all? | For trace-level debugging, latency charts, and span replay in the Arize UI. It is the observability layer, not the metric computation layer |
| Could Arize replace our metric computation? | Partially — Arize's evaluation framework could compute some safety metrics. But fairness disparity metrics (demographic comparisons across decisions) require our custom logic |
| Where should RAI metric logic live? | In our Tier-2 jobs — we own the thresholds, the business definitions, and the compliance interpretation. Arize is a tool, not the source of truth |

---

## Summary

1. **AI apps emit OTel spans** — spans go to Arize (for observability) AND to a local JSONL file (for metric computation). These are two independent sinks.

2. **Provider SPI fetches spans** from the local file (today) or directly from Arize (future, when ArizeProvider is activated). The rest of the system never knows which.

3. **Tier-2 jobs compute metrics** in Python — statistical comparisons on span attributes. 12 metrics, every 15 minutes, written to SQLite.

4. **The enterprise architect is right:** The Assurance API currently reads SQLite directly, bypassing the SPI. The fix is to extend the SPI with `fetch_metrics()` and have LocalProvider wrap the SQLite reads. The API then calls `provider.fetch_metrics()` — decoupled from storage for good.

5. **Arize is observability, not evaluation** — in our current architecture, all RAI metric computation is ours.

---

*Built on: Python 3.12 · FastAPI · LangGraph · OpenTelemetry · Arize · APScheduler · Streamlit*
