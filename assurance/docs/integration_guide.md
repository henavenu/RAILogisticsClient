# RAI Assurance Service — Architecture & Integration Guide

## What this service does

The RAI Assurance Service is the observability and accountability layer for the RAI Trust Platform.
It pulls spans (traces) from Arize cloud, computes 13 fairness/safety/reliability/privacy metrics,
stores results in SQLite (or PostgreSQL), and exposes them via a FastAPI + Streamlit dashboard.

---

## Project structure

```
rai-assurance/
├── core/                        # Shared foundation — import from here, not from siblings
│   ├── config.py                # Single Settings object; reads .env once at startup
│   ├── logging.py               # Structured logger factory
│   ├── models/
│   │   └── metric_result.py     # MetricResult — the universal result object
│   ├── providers/               # Provider SPI (swappable data sources)
│   │   ├── base.py              # AssuranceProvider ABC
│   │   ├── arize_provider.py    # Pulls live OTel spans from Arize cloud
│   │   ├── local_provider.py    # Reads data/spans.jsonl (dev/offline)
│   │   └── mock_provider.py     # Deterministic canned data (used in tests)
│   ├── provider_registry.py     # Central registry; reads ASSURANCE_PROVIDER from .env
│   └── store/                   # Store SPI (swappable persistence)
│       ├── base.py              # AbstractStore ABC
│       ├── sqlite_store.py      # Default — data/metrics.db
│       ├── postgres_store.py    # Production placeholder
│       └── store_registry.py    # Lazy singleton; reads STORE_BACKEND from .env
│
├── metrics/
│   └── tier2metrics.py          # 12 metric functions + run_cycle()
│
├── evals/
│   └── evals.py                 # PII regex eval; LLM evals stubbed (haiku/gpt-5)
│
├── api/
│   └── main.py                  # FastAPI — 4 pillars, /metrics, /jobs/run, /healthz
│
├── dashboard/
│   └── app.py                   # Streamlit — 4-column live dashboard
│
├── rai_client/                  # LangGraph use-case apps that send traces TO Arize
│   ├── use_cases/
│   │   ├── loan_decision.py
│   │   └── support_chat.py
│   └── runner.py                # Sends a batch of loan + support traces
│
├── scripts/
│   ├── smoke.py                 # One-shot: fire an LLM call, verify trace lands in Arize
│   └── daily_report.py          # Prints a formatted 4-pillar health report via live API
│
└── tests/
    ├── conftest.py              # Forces ASSURANCE_PROVIDER=mock for all tests
    ├── test_metrics.py          # 16 metric unit tests
    ├── test_evals.py            # 7 PII eval unit tests
    └── test_api.py              # 9 API integration tests
```

---

## How Sharad's guardrail-service merges in

### What Sharad's service likely owns

| Sharad's concern | What it does |
|---|---|
| Guardrail providers | Azure Content Safety, OpenAI Moderation, custom rule engines |
| Guardrail results | A GuardrailResult model (pass/fail + score + category) |
| Guardrail metrics | Blocked request rate, category breakdown, latency |
| Guardrail API | Routes that proxy or log guardrail decisions |

### Common folders (shared, not duplicated)

| Folder | Who owns it | What Sharad adds |
|---|---|---|
| `core/config.py` | RAI Assurance | Sharad adds `AZURE_CS_KEY`, `AZURE_CS_ENDPOINT` settings |
| `core/logging.py` | RAI Assurance | Used as-is — no changes needed |
| `core/models/` | RAI Assurance | Sharad adds `guardrail_result.py` alongside `metric_result.py` |
| `core/providers/` | RAI Assurance | Sharad adds `azure_content_safety_provider.py`, `openai_moderation_provider.py` and registers them in `provider_registry.py` |
| `core/store/` | RAI Assurance | Shared — Sharad's guardrail results write to the same store via `AbstractStore` |

### New folders Sharad brings in

```
guardrails/
├── __init__.py
├── azure_cs.py          # Calls Azure Content Safety API, returns GuardrailResult
├── openai_moderation.py # Calls OpenAI /moderations endpoint
└── run_guardrails.py    # Equivalent of run_cycle() — runs all guardrail checks

core/models/
└── guardrail_result.py  # New model alongside metric_result.py
```

### Provider registry change (one line)

```python
# core/provider_registry.py — Sharad adds his providers here
registry.register("arize",   ArizeProvider)
registry.register("local",   LocalProvider)
registry.register("mock",    MockProvider)
# ↓ Sharad adds:
registry.register("azure_cs",         AzureContentSafetyProvider)
registry.register("openai_moderation", OpenAIModerationProvider)
```

### API merge

Sharad adds a new pillar to the API (one dict entry in `api/main.py`):

```python
_PILLARS = {
    "fairness":    [...],  # RAI Assurance owns
    "safety":      [...],  # RAI Assurance owns
    "reliability": [...],  # RAI Assurance owns
    "privacy":     [...],  # RAI Assurance owns
    "guardrails":  [       # ← Sharad adds
        "guardrail_block_rate",
        "guardrail_latency_p50_ms",
        "guardrail_category_breakdown",
    ],
}
```

And a new job route:

```
POST /jobs/guardrails   ← Sharad's equivalent of /jobs/run
GET  /guardrails        ← Sharad's results list
```

### .env additions from Sharad

```env
# Sharad's guardrail providers
AZURE_CS_KEY=...
AZURE_CS_ENDPOINT=https://<name>.cognitiveservices.azure.com/
GUARDRAIL_PROVIDER=azure_cs   # azure_cs | openai_moderation | mock
```

---

## Integration steps (for Sharad)

1. **Clone this repo** — all the foundation is already here
2. **Add your provider files** to `core/providers/` and register in `core/provider_registry.py`
3. **Add `guardrail_result.py`** to `core/models/`
4. **Create `guardrails/` folder** with your computation functions (mirrors `metrics/tier2metrics.py`)
5. **Add your metrics to `api/main.py`** — one new entry in `_PILLARS`, two new routes
6. **Add your settings** to `core/config.py` and `.env`
7. **Write tests** in `tests/` — use `conftest.py` pattern (MockProvider, STORE_BACKEND=sqlite)
8. **Add your dashboard column** in `dashboard/app.py` — change `st.columns(4)` to `st.columns(5)`

No changes needed to `core/store/`, `core/logging.py`, or the existing metrics/evals.

---

## Data flow (end to end)

```
rai_client/runner.py
    │  (LangGraph apps emit OTel spans with rai.* attributes)
    ▼
Arize cloud (project: rai-assurance)
    │  (ArizeProvider.fetch_spans pulls last N hours via ArizeClient.spans.export_to_df)
    ▼
metrics/tier2metrics.py  +  evals/evals.py
    │  (metric functions compute values; PII regex scans span text)
    ▼
core/store/sqlite_store.py  →  data/metrics.db
    │
    ▼
api/main.py  →  GET /metrics/summary  →  dashboard/app.py
```

---

## Key design decisions

| Decision | Why |
|---|---|
| Provider SPI (abstract base class) | Swap Arize → Phoenix → Datadog via `.env` only, no code change |
| Store SPI | SQLite for dev, PostgreSQL for shared prod DB between RAI and guardrail services |
| MockProvider in tests | Tests never call real Arize or OpenAI — fast, free, deterministic |
| Regex PII (not LLM) | Free, instant, no API cost — catches emails, SSN, phone, card numbers |
| `since_hours` parameter | All fetch calls are time-windowed; default 1 hour for production use |
| `run_cycle()` as single entry point | API, scripts, and scheduler all go through one function |
