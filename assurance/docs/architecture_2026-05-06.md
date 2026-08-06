# RAI Assurance — Architecture & Build Notes (2026-05-06)

This doc covers the four major changes shipped today plus the React dashboard.
It's the resume-from-here reference for the next session and the demo-day cheat sheet.

---

## 1. Quick start

You need **three terminals**. Activate venv in any terminal that runs Python.

```bash
# Terminal 1 — FastAPI (backend)
cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-assurance"
source .venv/bin/activate
uvicorn api.main:app --reload --port 8000

# Terminal 2 — Next.js (React dashboard)
cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-assurance/dashboard-react"
npm run dev

# Terminal 3 — emit fresh Arize traces (one-shot, only when window goes stale)
cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-assurance"
source .venv/bin/activate
python -m rai_client.runner
```

Open: **http://localhost:3000** (React dashboard)

Optional fallback: **http://localhost:8501** (Streamlit) — keep alive for quick API debugging.

---

## 2. What changed today (before vs after)

| Concern | Before | After |
|---|---|---|
| **Live API perf** | `/metrics/summary` ran Phoenix LLM judging inline, ~60-90s per request, dashboard hung | Live API = **statistical only**, sub-2s. LLM evals moved to dedicated endpoint. |
| **LLM eval execution** | Sequential per-span loop (`for span: evaluator.evaluate(...)`) — one HTTP call at a time | Phoenix **batch** via `async_evaluate_dataframe(concurrency=10)`. ~10× speedup, auto-retry on transient errors. |
| **Bundled Arize evals** | None — only custom `create_classifier` prompts | Two **Phoenix bundled evaluators** wired in: `RefusalEvaluator` (Safety pillar) + `CorrectnessEvaluator` (Reliability pillar). |
| **Dashboard** | Streamlit 4-column view, auto-refreshes every 30s, hung the API | **Next.js 16 + React 19 + Tailwind 4** dark dashboard at :3000. Window selector (1/4/12/24h, default 1h). LLM Evals on separate `/llm-evals` page. Streamlit kept for debug. |
| **API loading `.env`** | Uvicorn didn't read `.env` — `ANTHROPIC_API_KEY` looked unset to API | Added `load_dotenv()` at top of `api/main.py`. |
| **Tests** | 38 passing | 43 passing (5 new for refusal + correctness). |

---

## 3. Backend architecture

### 3.1 Two API surfaces, two speeds

The backend now exposes two distinct surfaces with different speed/freshness profiles:

```
                ┌──────────────────────────────────────┐
                │         GET /metrics/summary         │   ← Sharad's dashboard
                │     (statistical + PII regex only)   │     consumes this for
HOT PATH        │            sub-2s response           │     live tiles
                └────────────┬─────────────────────────┘
                             │ ArizeProvider.fetch_spans()
                             │ + tier2metrics.run_cycle()
                             │ + evals.run_evals(skip-LLM)
                             ↓
                       Arize Trace Store
                             ↑
                             │ ArizeProvider.fetch_spans()
                             │ + evals.run_llm_evals_window()
                             │ + Phoenix async_evaluate_dataframe
COLD PATH       ┌────────────┴─────────────────────────┐
                │           GET /llm-evals             │   ← LLM Eval Deep Dive
                │  (Phoenix LLM-as-judge, ~5-15s)      │     tab, on demand
                │  4 evaluators × concurrency=10       │
                │  Returns rates + per-span judgments  │
                │  + LLM explanations                  │
                └──────────────────────────────────────┘
```

**Why split:** A live dashboard polls every few seconds. Inlining LLM judging at every poll is wasteful (cost) and slow (UX). The deep-dive view runs LLM evals on user click and shows the full reasoning. This pattern matches the Arize/Phoenix recommended split between fast tier-2 metrics and lagging LLM evals.

### 3.2 Pillars and metrics inventory

**Live pillar API (`/metrics/summary`)** — 13 metrics, all statistical:

| Pillar | Metrics |
|---|---|
| Fairness (4) | `approval_rate_disparity_by_gender`, `denial_rate_disparity_by_gender`, `risk_score_disparity_by_gender`, `approval_rate_disparity_by_age` |
| Safety (4) | `toxicity_rate`, `offensive_content_rate`, `escalation_rate`, `negative_sentiment_rate` |
| Reliability (4) | `decisioning_latency_p50_ms`, `conversational_latency_p50_ms`, `decisioning_score_consistency`, `conversational_safe_interaction_rate` |
| Privacy (1) | `pii_detection_rate` |

**LLM evals (`/llm-evals`)** — 4 metrics, all LLM-judge:

| Metric | Type | Pillar | Phoenix construct |
|---|---|---|---|
| `demographic_bias_rate` | Custom prompt | Fairness | `create_classifier` with our prompt |
| `decision_fairness_rate` | Custom prompt | Fairness | `create_classifier` with our prompt |
| `refusal_rate` | **Bundled** | Safety | `phoenix.evals.metrics.RefusalEvaluator` |
| `correctness_rate` | **Bundled** | Reliability | `phoenix.evals.metrics.CorrectnessEvaluator` |

### 3.3 Phoenix integration deep dive

**Two Phoenix patterns we use:**

```python
# Custom evaluator — for domain-specific judgments where no bundled template fits.
# Lives in evals/evals.py:_demographic_bias_evaluator and _decision_fairness_evaluator.
from phoenix.evals import create_classifier
ev = create_classifier(
    name="demographic_bias",
    prompt_template=_DEMOGRAPHIC_BIAS_PROMPT,
    llm=_judge_llm(),                     # claude-haiku-4-5 via JUDGE_MODEL env
    choices={"biased": 1, "unbiased": 0},
    direction="minimize",
)

# Bundled evaluator — for canonical evals shipped by Phoenix.
# Lives in evals/evals.py:_refusal_evaluator and _correctness_evaluator.
from phoenix.evals.metrics import RefusalEvaluator, CorrectnessEvaluator
ev = RefusalEvaluator(llm=_judge_llm())   # labels: refused | answered, neutral direction
ev = CorrectnessEvaluator(llm=_judge_llm())  # labels: correct | incorrect, maximize
```

**Batch execution (the speedup):**

```python
# evals/evals.py:_batch_judge — all four judge_*() functions route through this.
import asyncio, pandas as pd
from phoenix.evals import async_evaluate_dataframe

df = pd.DataFrame([eval_input_dict for ...])    # one row per candidate span
out_df = asyncio.run(async_evaluate_dataframe(
    dataframe=df,
    evaluators=[ev],
    concurrency=int(os.getenv("EVAL_CONCURRENCY", "10")),
    hide_tqdm_bar=True,
    exit_on_error=False,                # don't abort whole batch on one failure
))
# out_df columns: original input + f"{ev.name}_score" (Score dict per row) + f"{ev.name}_execution_details"
```

**Key facts:**
- Phoenix's `LLM(provider="anthropic", model=..., api_key=...)` does **NOT** auto-read `ANTHROPIC_API_KEY` env. We pass it explicitly.
- The `_score` column contains **Python dicts** (not JSON strings, despite the docstring claim) — `score.to_dict()` output. Each dict has `name, score, label, explanation, metadata, kind, direction`.
- `exit_on_error=False` + Phoenix's built-in retry loop (default `max_retries=10`) means transient `APIConnectionError` is automatically retried. We saw this fire and recover during live testing.
- `asyncio.run()` is safe inside FastAPI sync handlers (which run in anyio threadpool) — each call creates a fresh event loop. Would NOT be safe inside an async handler.

### 3.4 Env gating (so the live API stays fast)

`api/main.py` sets these defaults at module import (after `load_dotenv()` so user `.env` still wins):

```python
os.environ.setdefault("EVAL_FAIRNESS",    "false")
os.environ.setdefault("EVAL_SAFETY",      "false")
os.environ.setdefault("EVAL_RELIABILITY", "false")
```

`evals.run_evals()` checks these gates and skips LLM judging in the live `/metrics/summary` path. The `/llm-evals` endpoint **explicitly** passes `with_fairness=True, with_safety=True, with_reliability=True` to override.

`tests/conftest.py` sets all three to `"false"` so test runs never hit the live LLM.

### 3.5 API endpoints

| Method | Path | Purpose | Speed |
|---|---|---|---|
| GET | `/healthz` | Liveness check | instant |
| GET | `/metrics/summary?window=N&metric_type=fairness\|safety\|reliability\|privacy` | Pillar summary, used by dashboard | <2s |
| GET | `/metrics?window=N&metric_type=...` | Flat list of all 13 statistical metrics | <2s |
| GET | `/metrics/{name}?window=N` | Single metric by name | <2s |
| POST | `/jobs/run?since_hours=N` | Trigger a metrics cycle (test/back-compat) | <2s |
| GET | `/llm-evals?window=N` | Run all 4 LLM-judge evals + return judgments with explanations | 5-15s |

`window` defaults to `1`, accepts 1-168 hours.

CORS allows `http://localhost:3000` and `http://127.0.0.1:3000` (the Next.js dev server).

### 3.6 File map (backend)

```
api/main.py                      ─ FastAPI app, all endpoints
core/
├── models/metric_result.py      ─ MetricResult Pydantic model (status, threshold, breakdown, ...)
├── providers/arize_provider.py  ─ Arize span fetcher with bool/timestamp coercion
└── provider_registry.py         ─ Provider SPI registry (arize|local|mock via ASSURANCE_PROVIDER env)
evals/evals.py                   ─ All evals — PII regex, fairness custom, safety/reliability bundled
                                   Key functions: pii_detection_rate, judge_*, *_rate, run_evals,
                                   run_llm_evals_window, run_pii_eval_window, run_fairness_eval_window
                                   Batch dispatcher: _batch_judge
metrics/tier2metrics.py          ─ 12 statistical metrics + run_cycle()
rai_client/
├── runner.py                    ─ Emits demo spans (loan + support, including biased/PII test cases)
├── simulator.py                 ─ Variant runner
└── use_cases/                   ─ LangGraph apps for loan_decision and support_chat
scripts/
├── run_cycle.py                 ─ One-shot statistical metrics cycle
├── pii_eval.py                  ─ One-shot PII eval over an Arize window
├── fairness_eval.py             ─ One-shot fairness LLM eval over an Arize window
├── daily_report.py              ─ Reporting harness
└── smoke.py                     ─ Tracing smoke test
tests/
├── conftest.py                  ─ Sets ASSURANCE_PROVIDER=mock, disables EVAL_*
├── test_metrics.py              ─ 15 tests for tier2 statistical metrics
├── test_evals.py                ─ 19 tests for PII + fairness + bundled (stubbed judgments)
└── test_api.py                  ─ 9 tests for endpoint shapes and pillar counts
tracing.py                       ─ OTEL tracing setup
.env                             ─ ARIZE_API_KEY, ARIZE_SPACE_ID, ANTHROPIC_API_KEY,
                                   JUDGE_MODEL=claude-haiku-4-5, ASSURANCE_PROVIDER=arize, ...
```

---

## 4. React dashboard architecture

### 4.1 Stack

- **Next.js 16.2.5** with App Router + Turbopack (default in Next 16)
- **React 19.2.4**
- **Tailwind CSS 4** (CSS-driven config via `@theme` block — no `tailwind.config.js`)
- **Recharts 3.8** for radar + donut charts
- **lucide-react** for icons
- **clsx + tailwind-merge** for class composition

Installed via `create-next-app@latest` with flags: `--typescript --tailwind --app --no-src-dir --eslint --import-alias "@/*" --use-npm --turbopack`.

### 4.2 Color palette (matches Sharad's aesthetic)

Defined as CSS variables in `app/globals.css`, surfaced through Tailwind via `@theme inline`:

| Token | Value | Use |
|---|---|---|
| `--bg` | `#060a10` | App background |
| `--surface` | `#0e131a` | Cards |
| `--surface-elevated` | `#161c26` | Inputs, hover states |
| `--border` | `#1f2733` | Card borders, dividers |
| `--text-primary` | `#e8ecef` | Main text |
| `--text-secondary` | `#94a3b8` | Labels, secondary |
| `--text-muted` | `#64748b` | Tertiary, metadata |
| `--green` / `--green-bright` | `#22c55e` / `#4ade80` | OK status, "Pass" |
| `--amber` | `#f59e0b` | WARNING |
| `--red` | `#ef4444` | CRITICAL, alerts |
| `--cyan` | `#06b6d4` | Info badges |
| `--violet` | `#a78bfa` | **Reserved for our Assurance Layer** — visually distinct from Sharad's chrome |

### 4.3 Page layout

**`/` — Main integrated dashboard:**

```
┌─────────────────────────────────────────────────────────────────┐
│  App Stack | Rules | Live Tail | Privacy                        │
│  ● RESPONSIBLE AI DASHBOARD  Updated 09:58:25                   │
│  [Overview][LLM Evals]              [App▾] [Window 1h▾] [Refresh]│
├─────────────────────────────────────────────────────────────────┤
│ App Trust │ Violations │ Tokens │ Evals/Min │ Block │ Alerts    │  ← KPI strip (placeholder)
├──────────┬───────────────────────────────┬───────────────────────┤
│          │                                │                     │
│ POLICY   │  VERDICT     EVALUATION        │  ✦ RAI ASSURANCE    │
│ EVALS    │  DONUT       DISTRIBUTION       │    LAYER · TIER 2   │  ← OUR REAL VALUES
│ (left)   │                                │  • Radar           │     (right rail,
│          │  TIER 1 GOVERNANCE CARDS        │  • Pillar cards     │      violet accent)
│          │  (3-col grid)                   │  • Deep-dive link   │
└──────────┴───────────────────────────────┴───────────────────────┘
│ ● Streaming T1 · Lagging T2/T3 · Config driven                  │
└─────────────────────────────────────────────────────────────────┘
```

**`/llm-evals` — LLM-judge deep-dive page:**

```
┌─────────────────────────────────────────────────────────────────┐
│ [same header — window selector applies here too]                │
├─────────────────────────────────────────────────────────────────┤
│  ✦ LLM Eval Deep Dive          [▶ Run LLM Evals (1h)]          │
│  Phoenix custom + bundled. Batch concurrent.                     │
│                                                                  │
│  ┌───────────────────┐ ┌──────────────────────────┐             │
│  │ DEMOGRAPHIC BIAS  │ │ DECISION FAIRNESS        │             │
│  │ [CUSTOM][Fairness]│ │ [CUSTOM][Fairness]       │             │
│  │  100.0%   warning │ │  27.3%   warning         │             │
│  │  ▶ Show 2 judgments + explanations               │           │
│  └───────────────────┘ └──────────────────────────┘             │
│  ┌───────────────────┐ ┌──────────────────────────┐             │
│  │ REFUSAL           │ │ CORRECTNESS              │             │
│  │ [BUNDLED][Safety] │ │ [BUNDLED][Reliability]   │             │
│  │  50.0%   ok       │ │  0.0%    warning         │             │
│  └───────────────────┘ └──────────────────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

Per-metric expansion shows the full per-span list with the LLM judge's free-text explanation.

### 4.4 React file map

```
dashboard-react/
├── app/
│   ├── layout.tsx              ─ Root, dark theme, fonts
│   ├── globals.css             ─ Color palette, scrollbar, animations
│   ├── page.tsx                ─ Main dashboard route ('use client')
│   └── llm-evals/page.tsx      ─ LLM deep-dive route ('use client')
├── components/
│   ├── ui/
│   │   ├── Card.tsx            ─ Card + CardHeader (reusable container)
│   │   ├── Badge.tsx           ─ Status pills + StatusDot
│   │   └── Spinner.tsx         ─ Loading indicator
│   └── dashboard/
│       ├── Header.tsx          ─ Top bar: title, sub-tabs, App, Window selector, Refresh
│       ├── KpiStrip.tsx        ─ 6 KPI cards (placeholder — Sharad's part)
│       ├── PolicyEvalsRail.tsx ─ Left rail (placeholder — Sharad's part)
│       ├── CenterPanels.tsx    ─ Verdict donut + eval bars + Tier 1 cards (placeholder)
│       ├── AssuranceRail.tsx   ─ ★ Right rail with REAL API values — the demo centerpiece
│       └── Footer.tsx          ─ "Streaming T1 · Lagging T2/T3"
└── lib/
    ├── api.ts                  ─ fetchSummary(window), fetchLlmEvals(window)
    ├── types.ts                ─ TS types matching API responses
    └── cn.ts                   ─ classname util (clsx + tailwind-merge)
```

### 4.5 Data flow

1. Page mounts → `useEffect` triggers `fetchSummary(window=1)`
2. `lib/api.ts` calls `http://localhost:8000/metrics/summary?window=1` (CORS allowed)
3. State updates → React re-renders → `AssuranceRail` shows pillar cards with real values
4. User clicks window dropdown → `setWindow(4)` → useEffect re-runs → refetch
5. User clicks "LLM Eval Deep Dive →" → navigates to `/llm-evals`
6. On `/llm-evals`, user clicks "Run LLM Evals" → `fetchLlmEvals(window)` → 5-15s spinner → renders metric cards + judgments

`process.env.NEXT_PUBLIC_API_URL` overrides the API base if set; defaults to `http://localhost:8000`.

### 4.6 Reading the radar chart in the Assurance Layer

The diamond/quadrilateral inside the **Assurance Health** card is a radar (spider) chart with four axes — one per pillar:

```
              Fairness
                 ▲
                 │
   Privacy ◀────┼────▶ Safety
                 │
                 ▼
              Reliability
```

**Distance from center on each axis = pillar health.**

| Pillar status | Plotted score | Visual meaning |
|---|---|---|
| `ok` | 100 | point at outer ring |
| `warning` | 50 | point pulled halfway in |
| `critical` | 20 | point near center |

(See `components/dashboard/AssuranceRail.tsx:154` — `score()` mapping.)

**How to read the shape:**
- **Big balanced shape** → all four pillars healthy (full diamond touching the outer ring).
- **Lopsided shape** → trouble concentrated in specific pillars.
- **Tiny shape near center** → system in widespread distress.
- **Pulled-in corner** = the pillar at that corner has warnings or criticals.

**Example:** If the diamond stretches horizontally (Privacy + Safety reach the outer ring) but is pinched vertically (Fairness + Reliability points pulled toward the center), it means Privacy and Safety are OK while Fairness and Reliability have warnings. The pillar cards below the radar show the actual metric values driving each pillar's status.

**Tracks the App filter.** When you change the App dropdown, the radar recomputes each pillar's status from the metrics visible under the filter (same logic as the pillar card badges). If a pillar has no metrics applicable to the selected app, its corner collapses to the center (score 0). So if you pick `loan_decision`, the Safety axis caves in (Safety metrics are all support-only); pick `support_chat` and the Fairness axis caves in (statistical fairness disparities are loan-only).

The radar is presentational only — for one-glance triage. The signal lives in the pillar cards (real metric values, breakdowns, thresholds, span counts).

### 4.7 App filter dropdown

The "App" dropdown in the header (`All Apps` / `loan_decision` / `support_chat`) filters which metrics are shown in the Assurance Layer right rail and on `/llm-evals`. It maps each metric to the use-case (RAI app) that produces its source spans, defined in `lib/metricApps.ts`.

| App selected | What's visible |
|---|---|
| **All Apps** | Every metric — full pillar inventory (default). |
| **loan_decision** | Loan-related metrics only: 4 fairness disparities (gender/age), decisioning latency p50, decisioning score consistency, PII detection (cross-cutting), and the `decision_fairness_rate` LLM eval. |
| **support_chat** | Support-related metrics only: 4 safety metrics (toxicity/offensive/escalation/sentiment), conversational latency, conversational safe-interaction rate, PII detection, and three LLM evals (`demographic_bias_rate`, `refusal_rate`, `correctness_rate`). |

**Implementation notes:**

- **Client-side only.** The backend `/metrics/summary` returns all metrics regardless of the dropdown — the React app filters which to render. No additional API surface, no extra round trips.
- **Pillar status badge dynamically recomputes** from the visible metrics. If a pillar's only warnings are filtered out, the badge flips back to OK. If a pillar has zero visible metrics under the filter, the badge shows `n/a`.
- **Radar chart also tracks the filter.** Each pillar's score on the radar is computed from the same filtered metric set as the pillar card. Pillars with no metrics applicable to the selected app collapse their radar corner to score 0. Shared helper: `filteredPillarView()` in `AssuranceRail.tsx`.
- **Hidden-count notice.** When a pillar card is expanded, a small italic line at the top says e.g. "1 metric hidden by app filter" so the user knows the view is filtered.
- **PII detection is `"both"`.** It scans both loan and support spans, so it remains visible regardless of the filter.

If you want the filter to reach the backend (true server-side `use_case` filtering on the Arize fetch), we'd add a `?app=loan_decision` query param to `/metrics/summary` and `/llm-evals` and pipe it through `provider.fetch_spans(use_case=...)`. Not done — current client-side filter is enough for the demo and avoids API contract churn for Sharad.

---

## 5. Demo runbook

### 5.1 Pre-demo setup

1. Start all three services (see Quick Start).
2. Run `python -m rai_client.runner` to ensure last hour has fresh spans.
3. Open `http://localhost:3000` in the browser.
4. Confirm right-rail Assurance Layer shows non-zero values.

### 5.2 Talking points (in order)

| # | Click / Point | Talking point |
|---|---|---|
| 1 | Show full `/` page | "Sharad owns the outer chrome — KPIs, policy evals, verdict distribution, Tier 1 cards. The right rail violet block is **our** Assurance Layer plugging into his Tier 2 slot." |
| 2 | Point at radar chart in Assurance Rail | "Live snapshot of the four assurance pillars — Fairness, Safety, Reliability, Privacy. Status driven by our 13 statistical metrics computed inline from Arize spans." |
| 3 | Click Fairness pillar card | "Drill-down shows the four disparity metrics with per-group breakdown. Statistical fairness — gender and age cohort spread." |
| 4 | Change window 1h → 4h → 1h | "Window selector controls the eval horizon. 1h = real-time, 24h = trend." |
| 5 | Click "LLM Eval Deep Dive →" | "Switching to the LLM-as-judge view. We separated this because LLM evals take 5-15s — they shouldn't block a live dashboard." |
| 6 | Click "Run LLM Evals" | "Running four evaluators concurrently via Phoenix's `async_evaluate_dataframe`. Two are **bundled** Arize evaluators — `RefusalEvaluator` and `CorrectnessEvaluator` — direct from `phoenix.evals.metrics`. Two are **custom** prompts we authored — Demographic Bias and Decision Fairness." |
| 7 | Expand a metric card | "Per-span judgment with the LLM's free-text explanation. This is the auditable evidence — every flag traces back to a specific span and a reasoned rationale." |
| 8 | Show the API directly (optional, for engineers) | `curl http://localhost:8000/metrics/summary?window=1` and `curl http://localhost:8000/llm-evals?window=1`. "Clean JSON contract for Sharad's dashboard or any consumer." |

### 5.3 Likely questions + answers

- **"Why not toxicity bundled?"** Phoenix v3 doesn't ship a toxicity template. Closest is `RefusalEvaluator`. We could add custom toxicity via `create_classifier` — same pattern as our bias prompts.
- **"Is the LLM evaluating itself?"** No. The runner traces are produced by gpt-4o-mini in the loan/support apps. The judge is Claude Haiku 4.5 (`JUDGE_MODEL` env). Independent model = real cross-validation, not a self-eval echo chamber.
- **"What about cost?"** Live API never invokes the judge. LLM evals only run when user explicitly clicks Run. Concurrency=10 means 4 evaluators × ~22 spans / 10 ≈ 9 LLM round-trips. Haiku pricing: pennies per run.
- **"Streaming?"** No, current path is request/response. Could add WebSocket streaming for incremental judgments — not needed for demo.

---

## 6. Configuration reference

### 6.1 Environment variables (`.env`)

| Var | Default | Purpose |
|---|---|---|
| `ASSURANCE_PROVIDER` | `arize` | `arize`/`local`/`mock` — span source |
| `ARIZE_API_KEY` | (required) | Arize API key |
| `ARIZE_SPACE_ID` | (required) | Arize workspace |
| `ANTHROPIC_API_KEY` | (required for LLM evals) | Claude API key for judge |
| `JUDGE_MODEL` | `claude-haiku-4-5` | Phoenix `LLM` model |
| `STORE_BACKEND` | `sqlite` | Legacy — store removed, kept for back-compat |
| `EVAL_FAIRNESS` | `true` (CLI) / `false` (API process) | Run demographic_bias + decision_fairness in `run_evals()` |
| `EVAL_SAFETY` | `true` (CLI) / `false` (API process) | Run refusal in `run_evals()` |
| `EVAL_RELIABILITY` | `true` (CLI) / `false` (API process) | Run correctness in `run_evals()` |
| `EVAL_CONCURRENCY` | `10` | Phoenix `async_evaluate_dataframe` concurrency |

The API process forces `EVAL_*=false` via `os.environ.setdefault()` after `load_dotenv()`. User `.env` still wins if explicitly set.

The Streamlit dashboard (`dashboard/app.py`) sets `EVAL_FAIRNESS=false` and friends locally too (it's been left mostly alone — debug-only now).

The React dashboard reads from `NEXT_PUBLIC_API_URL` (defaults to `http://localhost:8000`).

### 6.2 Ports

| Port | Service | Owner |
|---|---|---|
| 3000 | Next.js dev server | `dashboard-react/` |
| 8000 | FastAPI (uvicorn) | `api/` |
| 8501 | Streamlit (debug) | `dashboard/` |

---

## 7. Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Currently **43/43 passing**:
- `test_metrics.py` — 15 tests (tier-2 statistical aggregations)
- `test_evals.py` — 19 tests (PII regex, fairness with stub judgments, refusal/correctness with stub judgments)
- `test_api.py` — 9 tests (endpoint shapes, pillar counts, response models)

Tests never hit live LLM — `tests/conftest.py` forces `EVAL_*=false` and `ASSURANCE_PROVIDER=mock`. Adding new LLM-judge evals always means: add stub-judgment tests for the aggregator (`*_rate`) and don't test the judge function directly.

---

## 8. Known limitations + planned next steps

### Known issues

1. **Arize is dropping `rai.user_message` text on most support_chat spans.** Only `rai.user_message_len` survives the export. As a result, `judge_demographic_bias`, `judge_refusal`, and `judge_correctness` only see ~2 of 11 support spans per window. Investigation needed in tracing pipeline / OTEL exporter.
2. **`asyncio.run()` inside FastAPI sync handlers.** Works because endpoints are sync (anyio threadpool). If we ever upgrade to async endpoints, must switch to `await` directly. Easy to spot in `evals/evals.py:_batch_judge`.
3. **Phoenix v3 has no bundled toxicity evaluator.** Only `Refusal`, `Correctness`, `Faithfulness`, `Hallucination` (deprecated), `Conciseness`, `DocumentRelevance`. If toxicity is required, we'd add a custom `create_classifier` prompt — same architecture as our fairness evals.
4. **Streamlit dashboard auto-refreshes every 30s.** Wasteful given React is the main UI now. Either remove the auto-refresh or kill Streamlit entirely. Left alive intentionally as a fallback debug view.
5. **No CORS in production config.** Right now CORS allows `localhost:3000` only. When deploying, add the production origin.

### Next steps (per memory `project_progress.md`)

- **PII obfuscation before Arize OTEL sink** (Deliverable 2) — preprocessing layer that scans span attributes pre-export and replaces with `[REDACTED_*]` tokens. Lives in `tracing.py` / OTEL exporter wrapping. Separate from PII *detection*.
- **Investigate Arize span attribute drops.** Why is `rai.user_message` text dropping on support_chat exports?
- **Add custom toxicity prompt** (if needed) using the existing `create_classifier` pattern in `evals/evals.py`. Wire to Safety pillar as `toxicity_judge_rate`.
- **React dashboard polish:**
  - Auto-refresh option (toggle in Header)
  - URL-driven window selector (`?window=4` shareable)
  - Sparklines for trend over the last N hours
  - Client-side caching of `/llm-evals` results so a second click within a minute is instant
- **Production deploy** — Dockerfile for both FastAPI and Next.js; environment-aware CORS; auth.

---

## 9. Glossary

- **Tier 2 metrics** — Lagging quality metrics computed over a window (vs Tier 1 streaming flags). Statistical tier 2 = our 13 fast metrics. LLM-judge tier 2 = our 4 deep-dive metrics.
- **Phoenix** — Arize's open-source eval framework. We use `phoenix.evals.LLM`, `create_classifier`, bundled `RefusalEvaluator`/`CorrectnessEvaluator`, and `async_evaluate_dataframe`.
- **Disparity** — Statistical fairness metric measuring spread between demographic groups (e.g., max approval rate − min approval rate across genders).
- **Custom vs Bundled** — Custom = our own prompt template via `create_classifier`. Bundled = ships in `phoenix.evals.metrics` (Refusal, Correctness, Faithfulness, etc.).
- **Hot path / Cold path** — Hot = live, sub-second, called constantly (statistical metrics). Cold = on-demand, slow, called rarely (LLM evals).
