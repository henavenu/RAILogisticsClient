# RAI Assurance Service — API Integration Guide

| Field | Value |
|---|---|
| **Document type** | Integration Specification (consumer-facing) |
| **Audience** | PAP team, dashboard integrators, partner platforms |
| **Service version** | 0.5.0 |
| **API surface** | `/v1/*` |
| **Aligned with** | SRS §4.7 (RAI Assurance Service contract) |
| **Status** | Pre-soft-launch (May 25 internal demo · June 15 public soft launch) |
| **Repository** | https://github.com/henavenu/RAI-Trust-Platform |
| **Date** | 2026-05-12 |

---

## Table of contents

1. [Service overview](#1-service-overview)
2. [SRS compliance mapping](#2-srs-compliance-mapping)
3. [Architecture & flow](#3-architecture--flow)
4. [API contract](#4-api-contract)
5. [Metric catalog summary](#5-metric-catalog-summary)
6. [Quickstart examples](#6-quickstart-examples)
   - 6.6 Verified live response — `/v1/llm-evals`
   - 6.7 Verified live response — `/agents/bias-detection/invoke`
   - 6.8 **PAP integration checklist** ← start here, Sharad
7. [OpenAPI discovery surface](#7-openapi-discovery-surface)
8. [Deployment options](#8-deployment-options)
9. [Performance characteristics](#9-performance-characteristics)
10. [Security & compliance](#10-security--compliance)
11. [Versioning & deprecation](#11-versioning--deprecation)
    - 11.5 Common pitfalls (from initial integration)
12. [Support & escalation](#12-support--escalation)
13. [Glossary](#13-glossary)
14. [Appendix — screenshot index](#14-appendix--screenshot-index)
15. [Change log](#15-change-log)

---

## 1. Service overview

The RAI Assurance Service is a Responsible-AI observability and assurance API. It produces continuous metrics across **five pillars** — Fairness, Safety, Reliability, Privacy, and Quality — for any AI application that emits OTel spans to Arize AX. Consumers (dashboards, internal portals, downstream platforms) read these metrics through a single SRS-compliant `/v1/*` REST surface.

**What this service is:**
- A read-only metrics API designed to be plugged into a dashboard or compliance tooling
- The single source of truth for RAI signals across one or many applications (`app_id`-partitioned)
- Built to host pluggable specialist agents (bias detection, PII detection) behind an abstract interface

**What this service is not:**
- Not a verdict engine — `status`/`threshold` decisions live with the consumer (per SRS §4.7.2)
- Not a UI — bring your own dashboard
- Not a span emitter — applications emit OTel traces independently; this service consumes them

> 📸 *Screenshot placeholder — Swagger UI homepage at `/v1/docs`*

---

## 2. SRS compliance mapping

| SRS § | Requirement | Endpoint(s) | Status |
|---|---|---|---|
| §4.7.2 | Provider SPI with discovery | `GET /v1/catalog` | ✅ |
| §4.7.2 | Verdicts owned by consumer, not service | `MetricItem` has no `status`/`threshold` | ✅ |
| §4.7.3 | 6 mandatory metrics (hallucination, groundedness, response_relevance, toxicity_rate, prompt_injection, pii_exposure) | `GET /v1/metrics`, `GET /v1/llm-evals` | ✅ |
| §4.7.4 | Window presets `1h`, `1d`, `7d` + custom range | `GET /v1/metrics?window=...&since=...&until=...` | ✅ |
| §4.7.5 | All metrics scoped by `app_id` | Required query parameter on every metrics endpoint | ✅ |
| §3.1 | Single `data_status` enum (`ok` / `no_data` / `provider_unavailable` / `unsupported`) | Top-level + per-metric in every response | ✅ |
| §3.2 | Per-metric independence (one bad metric never fails the batch) | Each item in `metrics[]` resolves independently | ✅ |
| §3.3 | HTTP 200 unless service is down | 200/400/503 only | ✅ |
| §2.10 | Interactive OpenAPI docs in development | `GET /v1/docs`, `/v1/redoc`, `/v1/openapi.json` | ✅ |

A full gap-closure record is at `docs/remediation_plan.md`.

---

## 3. Architecture & flow

### 3.1 System architecture

```
┌──────────────────────┐
│  AI Application      │  (any LangGraph / LLM app)
│  Emits OTel spans    │
│  with `rai.app_id`   │
└───────────┬──────────┘
            │ OTel traces
            ▼
┌──────────────────────┐
│  Arize AX cloud      │  Trace storage + querying
└───────────┬──────────┘
            │ fetch_spans()
            ▼
┌──────────────────────────────────────────────────────────────┐
│  RAI Assurance Service  (FastAPI, port 3010)                 │
│                                                              │
│  ┌────────────────┐   ┌──────────────────┐   ┌────────────┐  │
│  │  /v1/catalog   │   │  /v1/metrics     │   │ /v1/llm-   │  │
│  │  discovery     │   │  fast statistical │   │ evals      │  │
│  └────────────────┘   └──────────────────┘   └─────┬──────┘  │
│                                                    │         │
│                                                    ▼         │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  Phoenix LLM-as-judge (Claude Haiku 4.5)                │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  Bias Detection Agent (SPI) — external bias agent microservice   │ │
│  │  /v1/agents/bias-detection                              │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────┬───────────────────────────────────────────────────┘
           │ JSON response (`/v1/*` contract)
           ▼
┌──────────────────────┐
│  PAP / dashboard     │  Consumer renders metrics
│  (you are here)      │
└──────────────────────┘
```

### 3.2 Request flow — fast statistical path

```
PAP                Assurance API           Arize cloud
 │                       │                       │
 │  GET /v1/metrics?     │                       │
 │  app_id=X&window=1h   │                       │
 │  &ids=a,b,c           │                       │
 ├──────────────────────►│                       │
 │                       │  fetch_spans(X, 1h)   │
 │                       ├──────────────────────►│
 │                       │  spans[]              │
 │                       │◄──────────────────────┤
 │                       │                       │
 │                       │  compute statistical  │
 │                       │  metrics (in-process) │
 │                       │                       │
 │  MetricsResponse      │                       │
 │  envelope             │                       │
 │◄──────────────────────┤                       │
 │                       │                       │
```

### 3.3 Request flow — LLM-as-judge deep path

```
PAP            Assurance API        Arize         Anthropic LLM
 │                  │                  │                  │
 │ GET /v1/llm-evals│                  │                  │
 │ ?app_id=X        │                  │                  │
 ├─────────────────►│                  │                  │
 │                  │  fetch_spans     │                  │
 │                  ├─────────────────►│                  │
 │                  │  spans[]         │                  │
 │                  │◄─────────────────┤                  │
 │                  │                                     │
 │                  │  Phoenix evaluator × 10             │
 │                  ├────────────────────────────────────►│
 │                  │  (concurrent batch, ~10s)           │
 │                  │  judgments + scores                 │
 │                  │◄────────────────────────────────────┤
 │                  │                                     │
 │ MetricsResponse  │                                     │
 │◄─────────────────┤                                     │
```

> 📸 *Screenshot placeholder — sample response in Swagger UI for `/v1/metrics`*

---

## 4. API contract

### 4.1 Base URL

| Environment | URL |
|---|---|
| Development (localhost) | `http://localhost:3010` |
| Staging (TBD) | `https://rai-assurance-stg.<host>` |
| Production (TBD) | `https://rai-assurance.<host>` |

### 4.2 Authentication

**Current state:** No authentication on the development build. Production will require an API key passed in the `Authorization: Bearer <token>` header. Key issuance process is being finalised — see §11 for status.

### 4.3 Endpoints

| # | Method · Path | Purpose | Latency |
|---|---|---|---|
| E1 | `GET /v1/healthz` | Liveness + Arize provider reachability | < 50 ms |
| E2 | `GET /v1/catalog` | Discovery — list all available `metric_id` values with units | < 50 ms |
| E3 | `GET /v1/metrics` | Read one or many metrics for an app + window (statistical only) | < 200 ms |
| E4 | `GET /v1/metrics/{metric_id}` | Convenience variant of E3 for a single metric | < 200 ms |
| E5 | `GET /v1/llm-evals` | Run all 10 Phoenix LLM-as-judge evaluators | 20–60 sec |
| E6 | `GET /agents/bias-detection` | Statistical bias detection via external bias agent microservice on live Arize spans | 1–3 sec |
| E7 | `POST /agents/bias-detection/invoke` | Bias detection on caller-supplied records (no Arize fetch) | 1–3 sec |

### 4.4 Common response envelope

Every metrics endpoint returns the same envelope shape:

```json
{
  "app_id": "novabridge-loan",
  "window": "24h",
  "data_status": "ok",
  "metric_count": 3,
  "metrics": [
    {
      "metric_id": "pii_detection_rate",
      "value": 0.32,
      "unit": "%",
      "data_status": "ok",
      "error": null
    },
    {
      "metric_id": "toxicity_rate",
      "value": null,
      "unit": "%",
      "data_status": "no_data",
      "error": null
    }
  ]
}
```

### 4.5 `data_status` semantics

| Value | Meaning | `value` |
|---|---|---|
| `ok` | Data resolved successfully | numeric |
| `no_data` | Window had zero matching spans | `null` |
| `provider_unavailable` | Upstream provider (Arize, external bias agent) failed | `null` |
| `unsupported` | Unknown `metric_id` requested | `null` |

The **top-level** `data_status` rolls up across the array; each item carries its own per-metric status. **One bad metric never fails the batch** (per SRS §3.2).

### 4.6 Query parameters

| Param | Endpoints | Required | Notes |
|---|---|---|---|
| `app_id` | E3 – E7 | ✅ | Application identifier. 1:1 with the Arize project name. |
| `ids` | E3, E5 | ❌ | Comma-separated `metric_id` filter. Default = all in catalog. |
| `window` | E3, E5, E6 | ❌ | Enum: `1h` / `6h` / `24h` / `7d`. Mutually exclusive with `since`/`until`. Default `1h`. |
| `since` | E3, E5 | ❌ | ISO-8601 UTC start (e.g. `2026-05-11T00:00:00Z`). Mutually exclusive with `window`. |
| `until` | E3, E5 | ❌ | ISO-8601 UTC end. Defaults to now. Max range from `since` = 7 days. |
| `provider` | E6, E7 | ❌ | Override bias-detection provider. Default `mock`. |

### 4.7 HTTP status codes

| Code | When |
|---|---|
| `200` | Always, when service can respond. Per-metric problems surface via `data_status`. |
| `400` | Malformed request (e.g. both `window` and `since` set; invalid ISO date) |
| `503` | Service down or critical dependency unreachable |

> 📸 *Screenshot placeholder — `Try it out` flow in Swagger UI for `GET /v1/metrics`*

---

## 5. Metric catalog summary

The `/v1/catalog` endpoint returns 21 metric IDs. Full per-metric specs (with LLM prompts) live in `docs/metrics_catalog.md`. Pillar summary:

| Pillar | Statistical | Regex | LLM-as-judge | Total |
|---|---|---|---|---|
| Fairness | 4 | — | 2 | 6 |
| Safety | 4 | — | 2 | 6 |
| Reliability | 4 | — | — | 4 |
| Privacy | — | 1 | 1 | 2 |
| Quality | — | — | 3 | 3 |
| **Total** | **12** | **1** | **8** | **21** |

Plus 2 internal evaluators (`refusal_rate`, `correctness_rate`) returned by `/v1/llm-evals`.

> 📸 *Screenshot placeholder — full catalog response in Swagger UI*

---

## 6. Quickstart examples

> Replace `https://rai-assurance.<host>` with the actual base URL for your environment. For local development, use `http://localhost:3010`.

### 6.1 Discover what's available

```bash
curl https://rai-assurance.<host>/v1/catalog
```

### 6.2 Pull a 24-hour view for a specific app

```bash
curl "https://rai-assurance.<host>/v1/metrics?app_id=novabridge-loan&window=24h&ids=pii_detection_rate,toxicity_rate"
```

### 6.3 Custom date range

```bash
curl "https://rai-assurance.<host>/v1/metrics?app_id=novabridge-loan&since=2026-05-10T00:00:00Z&until=2026-05-11T00:00:00Z"
```

### 6.4 Run all LLM-as-judge evaluators on demand

```bash
curl "https://rai-assurance.<host>/v1/llm-evals?app_id=novabridge-loan&window=24h"
```

### 6.5 JavaScript / TypeScript (PAP-style)

```typescript
const r = await fetch(
  `${ASSURANCE_BASE}/v1/metrics?app_id=novabridge-loan&window=24h&ids=hallucination_rate,toxicity_rate`
);
const body = await r.json();

body.metrics.forEach(m => {
  if (m.data_status === "ok") {
    renderCard(m.metric_id, `${m.value}${m.unit}`);
  } else if (m.data_status === "no_data") {
    renderCard(m.metric_id, "—");
  } else {
    renderCard(m.metric_id, "⚠️", m.error);
  }
});
```

> 📸 *Screenshot placeholder — sample PAP card rendering from the response*

### 6.6 Verified live response — `/v1/llm-evals` (captured 2026-05-11)

This is an actual response from the development environment with synthetic loan + support data:

```json
{
  "app_id": "rai-assurance",
  "window": "24h",
  "data_status": "ok",
  "metric_count": 10,
  "metrics": [
    {"metric_id": "correctness_rate",       "value": 0.0,   "unit": "%",     "data_status": "ok", "error": null},
    {"metric_id": "decision_fairness_rate", "value": 0.273, "unit": "%",     "data_status": "ok", "error": null},
    {"metric_id": "demographic_bias_rate",  "value": 1.0,   "unit": "%",     "data_status": "ok", "error": null},
    {"metric_id": "groundedness_score",     "value": 0.0,   "unit": "score", "data_status": "ok", "error": null},
    {"metric_id": "hallucination_rate",     "value": 0.5,   "unit": "%",     "data_status": "ok", "error": null},
    {"metric_id": "pii_exposure_rate",      "value": 0.0,   "unit": "%",     "data_status": "ok", "error": null},
    {"metric_id": "prompt_injection_rate",  "value": 0.0,   "unit": "%",     "data_status": "ok", "error": null},
    {"metric_id": "refusal_rate",           "value": 0.5,   "unit": "%",     "data_status": "ok", "error": null},
    {"metric_id": "response_relevance",     "value": 0.0,   "unit": "score", "data_status": "ok", "error": null},
    {"metric_id": "toxicity_judge_rate",    "value": 0.5,   "unit": "%",     "data_status": "ok", "error": null}
  ]
}
```

**Interpretation reference for PAP rendering logic:**
- `metric_count` always equals `len(metrics)`
- Sort order is alphabetical by `metric_id` (deterministic; safe to display as-is)
- For `score` units, **higher is better** (e.g. `groundedness_score: 0.0` is bad; `1.0` is ideal)
- For all other units, **lower is better** (rates closer to 0 = healthier)

### 6.7 Verified live response — `/agents/bias-detection/invoke` (captured 2026-05-11)

Bias detection against synthetic biased loan records:

```json
{
  "provider": "mock",
  "records_analyzed": 120,
  "report": {
    "provider": "mock",
    "overall_passed": false,
    "risk_level": "CRITICAL",
    "metrics": [
      {
        "protected_attribute": "gender",
        "privileged_group": "Male",
        "unprivileged_group": "Female",
        "privileged_rate": 0.629,
        "unprivileged_rate": 0.483,
        "metric_name": "Disparate Impact Ratio (DIR)",
        "metric_value": 0.767,
        "threshold": 0.8,
        "passed": false,
        "interpretation": "Adverse impact detected (4/5 rule). Female approval rate 48.3% vs Male 62.9%.",
        "severity": "HIGH"
      }
      // ... 9 more metrics across age, race
    ],
    "violations": [
      "GENDER (Female vs Male): Disparate Impact Ratio (DIR)=0.767 — Adverse impact detected (4/5 rule). ..."
    ],
    "recommendations": [
      "Audit gender features — remove proxies (e.g., title, first name).",
      "Audit race/ethnicity proxies (zip code, neighborhood). Apply disparate impact testing.",
      "Reweight age 40+ group or apply pre-processing debiasing (Reweighing algorithm)."
    ],
    "records_analyzed": 120,
    "elapsed_ms": 12.4
  }
}
```

---

## 6.8 PAP integration checklist

For Sharad / the PAP team, here's the concrete sequence to wire up against `/v1`:

### Phase A — Discovery
- [ ] Confirm base URL for target environment (dev / staging / prod)
- [ ] Call `GET /v1/healthz` — confirm `200` with `provider_status: "reachable"`
- [ ] Call `GET /v1/catalog` — fetch the 21 metric IDs PAP will render. Verify naming + units match what the dashboard expects.

### Phase B — Build the client
- [ ] Generate typed client (`openapi-typescript` or equivalent) from `/v1/openapi.json`
- [ ] Wrap `fetch` in a small `assuranceClient.ts` helper that handles all four `data_status` values uniformly
- [ ] Decide error policy: surface `error` field to operations dashboards but **never** to end users — sanitise to a friendly message

### Phase C — Render
- [ ] Wire **fast path** to `/v1/metrics` — refresh every 30 s, cache to avoid back-to-back duplicate requests
- [ ] Wire **deep path** to `/v1/llm-evals` — refresh every 15 min (expensive), show "Refresh" button for manual trigger
- [ ] Handle `data_status` in render logic:
  - `ok` → render value with unit
  - `no_data` → render `"—"` or empty state
  - `provider_unavailable` → render `"⚠️"` + a tooltip with `error`
  - `unsupported` → skip / hide (means PAP requested a metric the catalog doesn't expose)
- [ ] **For `score` units, color higher = better.** For `%` and others, color lower = better.

### Phase D — Polish + production
- [ ] Test against multiple `app_id` values (multi-tenant view)
- [ ] Test edge cases: empty data window, network failure, request timeout
- [ ] Add loading state for `/v1/llm-evals` (20–60s is real; users need feedback)
- [ ] Add Sentry / DataDog instrumentation around the API calls
- [ ] Document in PAP README which environment maps to which Assurance base URL

---

## 7. OpenAPI discovery surface

FastAPI auto-generates a complete machine-readable OpenAPI 3.x spec, plus two interactive renderings. **No additional library required.**

| URL | Format | Purpose | Audience |
|---|---|---|---|
| `/v1/openapi.json` | OpenAPI 3.x JSON | Machine-readable spec — source of truth for contract tests & client code generation | Code generators, CI |
| `/v1/docs` | Swagger UI (interactive HTML) | "Try it out" form for every endpoint; sends real requests and renders responses inline | Developers exploring the API |
| `/v1/redoc` | ReDoc (read-only HTML) | Cleaner reference rendering, easier to share as a spec link | Stakeholders reviewing the spec |

### 7.1 Generating a typed client

Because the OpenAPI spec is complete, any standard generator works:

```bash
# TypeScript / fetch-based
npx openapi-typescript https://rai-assurance.<host>/v1/openapi.json -o api-types.ts

# Python
pip install openapi-python-client
openapi-python-client generate --url https://rai-assurance.<host>/v1/openapi.json

# Go, Rust, Java, etc. — all supported via standard OpenAPI tooling
```

### 7.2 Disabling docs in production

For public deployment, set:
```python
docs_url=None
redoc_url=None
openapi_url=None
```
or guard with an environment flag. The spec endpoint should not be exposed on production unless behind auth.

> 📸 *Screenshot placeholder — `/v1/redoc` page rendering*

### 7.3 Schema preview — key components

A condensed view of the schemas you'll see in `/v1/openapi.json` (extracted from `api/v1/models.py`):

```yaml
components:
  schemas:
    DataStatus:
      type: string
      enum: [ok, no_data, provider_unavailable, unsupported]

    Unit:
      type: string
      enum: ["%", "score", "count", "ms", ""]

    MetricItem:
      type: object
      required: [metric_id, value, data_status]
      properties:
        metric_id:    { type: string }
        value:        { type: [number, "null"] }
        unit:         { $ref: "#/components/schemas/Unit" }
        data_status:  { $ref: "#/components/schemas/DataStatus" }
        error:        { type: [string, "null"] }

    MetricsResponse:
      type: object
      required: [app_id, window, data_status, metric_count, metrics]
      properties:
        app_id:        { type: string }
        window:        { type: string }       # "1h"|"6h"|"24h"|"7d" OR ISO range
        data_status:   { $ref: "#/components/schemas/DataStatus" }
        metric_count:  { type: integer }
        metrics:
          type: array
          items: { $ref: "#/components/schemas/MetricItem" }

    CatalogItem:
      type: object
      required: [metric_id, unit]
      properties:
        metric_id: { type: string }
        unit:      { $ref: "#/components/schemas/Unit" }

    CatalogResponse:
      type: object
      required: [metric_count, metrics]
      properties:
        metric_count: { type: integer }
        metrics:
          type: array
          items: { $ref: "#/components/schemas/CatalogItem" }

    HealthResponse:
      type: object
      required: [status, provider_status]
      properties:
        status:           { type: string, enum: [ok, degraded, down] }
        provider_status:  { type: string, enum: [reachable, unreachable, unknown] }
```

The full spec (including all paths, parameters, and example responses) is available at `/v1/openapi.json` while the service is running.

---

## 8. Deployment options

### 8.1 Comparison matrix

| Option | When to use | Pros | Cons | Effort to ship |
|---|---|---|---|---|
| **Docker container (on-prem)** | Strict data sovereignty; air-gapped environments; existing K8s footprint | Full control, no cloud lock-in, predictable cost | You run the ops (monitoring, scaling, TLS) | Medium — Dockerfile + Helm chart |
| **GCP Cloud Run** | Quick demos, low-volume staging, soft launch | Serverless, auto-scale to zero, pay-per-use, fastest path to a public URL | Cold start (~1-2s), GCP lock | Low — `gcloud run deploy` |
| **Azure Container Apps** | Microsoft shops; integration with Azure AD | Similar to Cloud Run, KEDA scaling, Azure ecosystem | Azure-only | Low |
| **AWS Fargate / ECS** | AWS shops, large traffic | AWS ecosystem, IAM integration | More setup than Cloud Run | Medium |
| **Kubernetes (self-managed)** | High-scale production, multi-region | Vendor-neutral, full HPA / observability stack | Highest ops complexity | High |
| **API Gateway in front** | Public-facing, multi-tenant, rate-limited | Auth, throttling, analytics out-of-box | Extra layer cost + latency | Low-Medium |

### 8.2 Recommended path

- **Soft launch (June 15):** Cloud Run or Azure Container Apps + Cloudflare/API gateway for auth. Fastest path to a working URL with reasonable ops cost.
- **Production scale-up:** Migrate to Kubernetes (GKE/AKS) when cross-region or HPA-driven scaling is needed.
- **Air-gapped enterprise (later):** Containerised Helm chart for self-hosted installs.

### 8.3 Required environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ARIZE_API_KEY` | ✅ | Arize personal API key |
| `ARIZE_SPACE_ID` | ✅ | Arize space identifier |
| `ANTHROPIC_API_KEY` | ✅ | Used by Phoenix LLM-as-judge (Claude Haiku 4.5) |
| `OPENAI_API_KEY` | optional | Required only if your apps' LangGraph use OpenAI |
| `ASSURANCE_PROVIDER` | optional | `arize` (default), `local`, `mock` |
| `JUDGE_MODEL` | optional | LLM-as-judge model (default `claude-haiku-4-5`) |
| `EVAL_FAIRNESS` / `EVAL_SAFETY` / `EVAL_QUALITY` / `EVAL_PRIVACY` / `EVAL_RELIABILITY` | optional | Gate LLM-as-judge categories on the fast `/v1/metrics` path (default `false` for cost containment) |
| `BIAS_AGENT_PROVIDER` | optional | `mock` (default) or `mock` |
| `BIAS_AGENT_BASE_URL` | optional | Default `http://localhost:8001` |
| `RAI_APP_ID` | optional | Default `rai-assurance`. Emitted as `rai.app_id` on every span. |

> 📸 *Screenshot placeholder — cloud-run deployment confirmation screen*

---

## 9. Performance characteristics

| Endpoint | p50 latency | p95 latency | LLM calls per request | $/request (est.) |
|---|---|---|---|---|
| `/v1/healthz` | 20 ms | 50 ms | 0 | $0 |
| `/v1/catalog` | 20 ms | 50 ms | 0 | $0 |
| `/v1/metrics` (statistical only) | 100 ms | 500 ms | 0 | $0 |
| `/v1/metrics` (with EVAL_QUALITY=true) | 5 s | 30 s | 10–20 | ~$0.01 |
| `/v1/llm-evals` | 20 s | 60 s | 80–150 | ~$0.05 |
| `/agents/bias-detection` (statistical) | 1 s | 3 s | 0 | $0 |

**Cost model:** LLM-as-judge dominates cost. Default deployment runs `/v1/metrics` as the fast path (no LLM) and exposes `/v1/llm-evals` for on-demand deep dives. For production, recommend caching `/v1/llm-evals` results with a 15-minute TTL so a refreshing dashboard doesn't re-bill on every refresh.

---

## 10. Security & compliance

- **TLS:** Required in staging and production. Deployment must terminate TLS at the gateway or load balancer.
- **PII in spans:** The service does **not** persist span content. Arize is the storage of record; this service reads on demand.
- **Secrets:** API keys (Arize, Anthropic) must be loaded from a secrets manager (GCP Secret Manager, Azure Key Vault, AWS Secrets Manager). Never bake into images.
- **Logging:** No PII logged. Span content stays in Arize. Internal request logs include `app_id`, `window`, `metric_id` — no raw record bodies.
- **Sanitised errors:** Per SRS §3.2, error strings must not leak credentials, AQL queries, or raw provider responses. Verified via `MetricItem.error` sanitisation in `api/v1/router.py`.

---

## 11. Versioning & deprecation

### 11.1 Current contract — `/v1/*`

- Stable, SRS-aligned, what PAP should consume.
- All new dashboards and downstream consumers should target `/v1/*` exclusively.

### 11.2 Legacy endpoints (still live, scheduled for removal)

These endpoints exist for backward compatibility with the original prototype dashboard. They are **not** SRS-compliant and should not be used for new integrations.

| Legacy path | Replacement | Notes |
|---|---|---|
| `GET /healthz` | `GET /v1/healthz` | Old shape: `{status, timestamp}`. New shape adds `provider_status`. |
| `GET /metrics/summary` | `GET /v1/metrics` + client-side grouping | Old endpoint returns pillar groupings; new endpoint returns flat list with `metric_id`. |
| `GET /metrics` | `GET /v1/metrics` | Old endpoint uses int `window` (hours), no `app_id`. |
| `GET /metrics/{name}` | `GET /v1/metrics/{metric_id}` | Path renamed; old endpoint omits `app_id` and `data_status`. |
| `POST /jobs/run` | (no replacement) | Internal cycle trigger; not part of consumer contract. |
| `GET /llm-evals` | `GET /v1/llm-evals` | Old endpoint omits `app_id` and the v1 envelope. |
| `GET /agents/bias-detection` | (no v1 namespace yet — stays at root) | Bias agent endpoints not yet migrated under `/v1/agents` — planned for next release. |
| `POST /agents/bias-detection/invoke` | (no v1 namespace yet — stays at root) | Same as above. |

**Deprecation timeline:** Legacy endpoints remain live for at least 60 days after the first production deployment. A formal deprecation notice will land in the release notes; expect actual removal in v0.7 or later.

### 11.3 Forward versioning policy

- **Breaking changes:** New major versions land under `/v2/*`. `/v1/*` remains supported for at least 12 months after `/v2/*` ships.
- **Additive changes:** New `metric_id` values, new optional response fields, new query parameters — all considered non-breaking and may land within `/v1/*`.
- **Authentication:** API-key auth (`Authorization: Bearer <token>`) lands ahead of production. Plan to rotate keys quarterly.

---

## 11.5 Common pitfalls (from initial integration)

These issues came up during early PAP testing. Documented so future integrators don't relearn them.

1. **Multi-line `curl` paste mangling**
   zsh and Terminal.app frequently mangle `\` line continuations. Always paste long `curl` commands as a single line, or use the prepackaged hydration script `scripts/calibrated_1k_corpus.py`.

2. **`/v1/metrics` returns `no_data` after a quiet window**
   The most common reason is that Arize has no spans in the requested window. Run `python -m rai_client.runner` to seed fresh traces, then retry.

3. **LLM-judge metrics return `no_data` despite spans existing**
   The Phoenix LLM-as-judge functions require **both** `rai.user_message` and `rai.response` attributes on each support_chat span. If Arize is dropping a field (we observed this with `rai.user_message`), the candidate list is empty and the metric returns `no_data` even when spans look present.

4. **`status` / `threshold` missing from v1 responses**
   Per SRS §4.7.2, verdict logic moved to PAP. If your PAP code expects `status` or `threshold` in the response, it's looking at the legacy `/metrics/*` shape. Switch to `/v1/metrics` and compute verdicts client-side.

7. **`app_id` is required on every `/v1/metrics` call**
   Forgetting it returns `422 Validation Error`. Default it client-side from the user's tenant context — never hardcode.

8. **`window` enum + `since`/`until` are mutually exclusive**
   Passing both returns `400`. Pick one. If you need a custom range, use `since`/`until` with ISO-8601 strings (e.g. `2026-05-11T00:00:00Z`); max range is 7 days.

---

## 12. Support & escalation

| Channel | Use for |
|---|---|
| **GitHub Issues** at https://github.com/henavenu/RAI-Trust-Platform/issues | Bug reports, feature requests |
| **Hena (`hena.venugopal@cognizant.com`)** | Service owner; integration & contract questions |
| **Sharad** | PAP-side integration; dashboard rendering questions |
| **#rai-assurance Slack channel** *(TBD)* | Day-to-day questions, deployment chatter |

**SLA targets (post-soft-launch):**
- Availability: 99.5% (staging), 99.9% (production)
- p95 latency on `/v1/metrics`: < 500 ms
- Mean time to acknowledge incident: < 30 minutes (business hours)

---

## 13. Glossary

| Term | Meaning |
|---|---|
| **Span** | One OpenTelemetry trace record emitted by the application, carrying attributes like `rai.gender`, `rai.decision`, `rai.user_message` |
| **`app_id`** | Application identifier. 1:1 with Arize project name (per architecture decision D1) |
| **Pillar** | One of five RAI categories: Fairness · Safety · Reliability · Privacy · Quality |
| **`metric_id`** | Unique identifier for a metric, e.g. `pii_detection_rate`, `hallucination_rate` |
| **LLM-as-judge** | Using a separate LLM (Claude Haiku 4.5) to evaluate the output of the application's primary LLM |
| **Phoenix** | Arize's open-source evaluation framework. Used for `create_classifier` and bundled evaluators |
| **Provider SPI** | Service-provider interface. Abstract class `AssuranceProvider` (for span fetching) and `BiasDetectionProvider` (for bias agents) — both swappable via env vars |
| **`data_status`** | The single signal indicating per-metric and overall response health: `ok` / `no_data` / `provider_unavailable` / `unsupported` |
| **PAP** | Policy & Assurance Platform — the consumer dashboard this guide is written for |

---

## 14. Appendix — screenshot index

When this document is exported as PDF or slide deck, the following placeholders should be replaced with actual screenshots:

| Section | What to capture |
|---|---|
| §1 | Swagger UI homepage at `http://localhost:3010/v1/docs` |
| §3.3 | Sample 200 response body in Swagger for `/v1/metrics` |
| §4.7 | "Try it out" execution result for `GET /v1/metrics?app_id=...` |
| §5 | Full `/v1/catalog` response showing all 21 metric IDs |
| §6.5 | Sample PAP dashboard card rendering (mocked HTML) |
| §7.2 | `/v1/redoc` page rendering |
| §8.3 | Cloud Run / Azure Container Apps deployment screen |

---

## 15. Change log

| Date | Author | Change |
|---|---|---|
| 2026-05-12 | Hena | Initial draft. Pre-soft-launch handover for Sharad / PAP integration. SRS §4.7 compliance mapping, architecture + flow diagrams, full endpoint reference, PAP integration checklist, OpenAPI schema preview, deployment options matrix, common pitfalls captured from initial integration. |
