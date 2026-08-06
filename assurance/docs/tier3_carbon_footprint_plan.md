# Tier 3 — Carbon Footprint Assurance Plan

**Status:** Draft for Hena's review · 2026-05-18
**Owner:** RAI Assurance (Hena)
**Implementation window:** Monday afternoon MVP, then iterate

---

## 0. TL;DR

Per-task carbon attribution across the multi-app, multi-agent fleet, surfaced at **six granularities** — call → agent → task → app → org → time series. We compute emissions from data the SDK already emits (tokens, model name, agent id, trace id), no SDK changes needed for Phase 1. Honest uncertainty band (±40%). MVP this afternoon = inference emissions + static grid intensity. Phase 2 wires marginal-intensity APIs (Electricity Maps / WattTime). Phase 3 adds embodied training emissions per SCI spec.

---

## 1. Why agents need their own carbon model

A chatbot turn is one inference call. An agent task is a **trajectory** — a planner LLM call, several tool invocations, a reflection step, sometimes retries. One LoanWise loan-request flow might fire 5–15 LLM calls plus tool calls before returning a decision. Reporting per-token or per-call hides this compounding. **Our functional unit is the completed user task** (= one root `trace_id`), so all the in-trajectory work attributes to the same number the customer cares about.

This aligns with the **SCI (Software Carbon Intensity)** spec from the Green Software Foundation:

> SCI = ((E × I) + M) / R
> where E = energy, I = carbon intensity, M = embodied emissions, R = functional unit

For us, R = "completed task" (one trace_id).

---

## 2. Scope & system boundary

**In scope (Phase 1, today):**
- Inference compute — every LLM call span: input tokens × `e_in` + output tokens × `e_out`
- Orchestration overhead — planner / router / reflection nodes count as LLM calls (they consume tokens)
- Tool calls — counted via `tool.name` span; energy estimated from latency × assumed power for external API calls (rough, but bounded)
- Context-window cost — handled implicitly via input token count (the linear term; we'll flag quadratic attention separately in Phase 2)

**Deferred (Phase 2):**
- Marginal grid intensity via API (Electricity Maps or WattTime)
- Quadratic attention surcharge for long-context calls (>32k tokens)
- Caching-detection lever — flag repeat tool calls within a task

**Deferred (Phase 3):**
- Embodied training emissions amortized per inference (needs per-model disclosure or estimate)
- Client-device energy (browser, mobile)
- Network transit

**Uncertainty band:** ±40% reported on every estimate. Per-token energy coefficients from public literature swing 2–5× across studies; grid intensity at fixed average understates marginal variance. **Tier 3 assurance is precisely about surfacing this uncertainty rather than papering over it** (Hena's research note).

---

## 3. The data we already have

Verified in `rai_trust_sdk/langgraph/middleware.py`:

| Span attribute | Source | What it gives us |
|---|---|---|
| `llm.model_name` | Set per turn (line 1643) | Model identifier → lookup energy coefficients |
| `llm.token_count.prompt` | Set per turn (line 1654) | Input tokens |
| `llm.token_count.completion` | Set per turn (line 1655) | Output tokens |
| `llm.token_count.total` | Set per turn (line 1656) | Sanity check |
| `rai.agent.id` | Set per node (line 1064) | LangGraph node name → agent attribution |
| `tool.name` | Set per tool span (line 859) | Tool invocations → tool emissions |
| `graph.node.name` | Set on root (line 345) | `rai.governance.turn` marker |
| OTel `trace_id` | Auto | Groups all spans for one user task |
| OTel `start_time` / `end_time` | Auto | Latency → power-time product for tool spans |
| Arize `project_name` | Set via `OTEL_RESOURCE_ATTRIBUTES` | 1:1 with `app_id` |

**Implication:** Phase 1 needs *zero SDK changes*. We pull spans from Arize (we already do), then do math.

---

## 4. The formula (Phase 1)

For each LLM-call span:

```
energy_wh = (prompt_tokens × e_in[model]) + (completion_tokens × e_out[model])
emissions_g = energy_wh × grid_intensity_g_per_wh × pue
```

For each tool-call span:

```
duration_h = (end_time - start_time) / 3.6e12     # nanoseconds → hours
energy_wh = duration_h × tool_power_w[tool_class] # assumed 50W avg for external API, 200W for code sandbox
emissions_g = energy_wh × grid_intensity_g_per_wh × pue
```

Roll-ups:

```
emissions_per_call    = above
emissions_per_agent   = Σ spans where rai.agent.id == X
emissions_per_task    = Σ spans where trace_id == T
emissions_per_app     = Σ tasks for app_id (= Arize project)
emissions_org_total   = Σ apps
```

### Phase 1 constants (literature-derived, ±40% honest)

**Per-token energy** (Wh per 1k tokens, prompt and completion treated separately because completion dominates):

| Model class | `e_in` (Wh/1k tok) | `e_out` (Wh/1k tok) | Source |
|---|---|---|---|
| GPT-4 / Claude Opus class (frontier) | 0.6 | 1.8 | OpenAI/Anthropic + academic estimates |
| GPT-3.5 / Claude Haiku / Llama 70B (mid) | 0.15 | 0.45 | EpochAI 2024, scaled |
| Llama 8B / Mistral / Groq-hosted small | 0.04 | 0.12 | Published Groq efficiency figures |

**Grid intensity** (`grid_intensity_g_per_wh`, Phase 1 = static):
- Default: **0.40 g/Wh** (= 400 g/kWh, US average grid 2024)
- Configurable via env: `CARBON_GRID_INTENSITY_G_PER_WH=0.40`

**PUE** (Power Usage Effectiveness — data-center overhead multiplier):
- Default: **1.2** (typical hyperscaler)
- Configurable: `CARBON_PUE=1.2`

**Tool power assumptions** (`tool_power_w`):
- External API call (search, vector DB): 50 W average
- Code-sandbox execution: 200 W average
- Local function call (`graph.node.name`-only, no `tool.name`): 0 W (orchestration overhead already in LLM tokens)

These constants live in `evals/carbon_coefficients.py` so they're tunable in one place. A research note next to each value documents the source so reviewers can challenge it.

---

## 5. Granularity matrix

| Level | What it answers | Aggregation key | Sample metric ID |
|---|---|---|---|
| **call** | "How much did this one LLM call emit?" | span_id | `carbon_emissions_per_call_g` |
| **agent** | "Which agent in the graph is the carbon hotspot?" | `rai.agent.id` | `carbon_emissions_per_agent_g_breakdown` |
| **task** | "What's the carbon cost of one completed user task?" | `trace_id` | `carbon_emissions_per_task_p50_g`, `_p95_g` |
| **app** | "What's LoanWise's total footprint this hour?" | Arize project (= `app_id`) | `carbon_emissions_app_total_kgco2e` |
| **org** | "Across all apps, what's our footprint?" | sum apps | `carbon_emissions_org_total_kgco2e` |
| **time series** | "How is the per-task footprint trending?" | bucket by hour or day | `carbon_emissions_per_task_p50_timeseries` |

---

## 6. API surface

**Option A — extend `/v1/metrics`** (lower friction, matches PAP contract):

Add new metric IDs to the existing response, all unit `gCO2e` or `kgCO2e`. PAP dashboard surfaces them under a new "Sustainability" pillar.

**Option B — new namespaced endpoint** `/v1/carbon/...`:

- `GET /v1/carbon/summary?app_id=X&window=1h` → headline rollups (per-task p50/p95, app total, agent breakdown)
- `GET /v1/carbon/task/{trace_id}` → drill into one task's per-call/per-agent attribution
- `GET /v1/carbon/agent-breakdown?app_id=X&window=24h` → ranked agent hotspots
- `GET /v1/carbon/timeseries?app_id=X&granularity=hour&hours=24` → trend
- `GET /v1/carbon/recommendations?app_id=X` → reduction levers (see §8)

**Recommendation: Option B.** Carbon is a distinct pillar with hierarchical drill-down needs the flat `/v1/metrics` shape doesn't fit cleanly. Keep `/v1/metrics` for the per-pillar scoreboard; let PAP link out from a "Sustainability" tile to the dedicated `/v1/carbon` views.

**Open question for review:** A or B? Sharad's PAP contract may have a preference.

---

## 7. Response shape (proposed)

```json
GET /v1/carbon/summary?app_id=loanapp&window=1h

{
  "app_id": "loanapp",
  "window": "1h",
  "tasks_observed": 47,
  "totals": {
    "emissions_g":          18.4,
    "emissions_kgco2e":     0.0184,
    "energy_wh":            46.0,
    "uncertainty_band_pct": 40
  },
  "per_task": {
    "p50_g":  0.31,
    "p95_g":  1.20,
    "max_g":  2.40,
    "min_g":  0.08
  },
  "per_agent_breakdown_g": {
    "router":           1.2,
    "intl_wire_agent":  8.6,
    "loan_decision":    5.1,
    "reflection":       3.5
  },
  "per_model_breakdown_g": {
    "llama-3.1-70b-versatile": 14.2,
    "llama-3.1-8b-instant":    4.2
  },
  "assumptions": {
    "grid_intensity_g_per_wh": 0.40,
    "grid_intensity_source":   "US_average_2024 (static — Phase 2 wires marginal)",
    "pue":                     1.2,
    "embodied_emissions":      "not included (Phase 3)"
  },
  "data_status": "ok"
}
```

The `assumptions` block is the **honesty footer** — every consumer sees what we assumed, so the ±40% band is contextualized, not hidden.

---

## 8. Reduction levers we'll surface

The plan's research note flagged: *"reduction recommendations tied to specific levers."* For each app, we compute and surface:

1. **Model right-sizing opportunity** — `% of tokens spent on frontier-class models on sub-tasks that could use a smaller model`. Heuristic: any call < 200 output tokens against a frontier model is a candidate for downscale.
2. **Iteration runaway** — `% of tasks exceeding N agent loop iterations`. Default N=10. Surfaces unbounded reflection trajectories.
3. **Context-cost growth** — `mean prompt tokens` over time; flag if growing >20% week-over-week (memory bloat).
4. **Tool caching opportunity** — `% of tool calls in a task that are duplicates by (tool.name, normalized input)`.
5. **Region / time shifting** (Phase 2 only) — surface when alternate region/hour would have lower marginal intensity.

These become `GET /v1/carbon/recommendations?app_id=X` returning a ranked list with estimated saving.

---

## 9. Multi-app, multi-agent — how it composes

Hena's note: *"there will be multiple apps which each are multi-agentic and display carbon footprint in all granularities."*

The 1:1 `app_id ↔ Arize project` decision (already locked) means:

- **Multi-app rollup** is trivial — loop over registered apps in PAP, sum their `/v1/carbon/summary`. `GET /v1/carbon/summary?app_id=*` returns org-wide.
- **Multi-agent rollup within an app** works because every LLM-call span carries `rai.agent.id`. The `per_agent_breakdown_g` dict in §7 is the per-app, per-agent view.
- **Cross-app agent comparison** (e.g., "does `router` cost more carbon in LoanWise than in MyBank?") is a derived view: query both apps, compare same `rai.agent.id`.

No new data model needed — same span attributes, different group-by.

---

## 10. The Tier 3 deliverable contents (per research note)

Mapping our implementation to what the research said a defensible Tier 3 report contains:

| Required content | Our delivery |
|---|---|
| System boundary diagram | §2 above + an architecture diagram in `docs/` showing what spans are summed |
| Per-task emissions with distribution (p50, p95, worst) | §7 `per_task` block |
| Comparison vs. baseline | Phase 2: human-task baseline; Phase 1 ships without |
| Reduction recommendations tied to levers | §8 + `/v1/carbon/recommendations` |
| Uncertainty quantification | §4 `assumptions` block + ±40% on every response |

---

## 11. MVP this afternoon — concrete checklist (2–3 hrs)

1. `evals/carbon_coefficients.py` — energy-per-token table, grid intensity, PUE, tool-power constants. ~30 min.
2. `evals/carbon_eval.py` — new evaluator that pulls spans (already done in `arize_provider.py`), maps each LLM/tool span → emissions_g, rolls up by trace_id / rai.agent.id / app. ~60 min.
3. `api/v1/carbon_router.py` — new sub-router with the §6 endpoints. ~45 min.
4. Wire into `api/main.py` and add to OpenAPI docs. ~10 min.
5. Smoke-test against the LoanWise data already in Arize (whatever the 1K batch leaves behind, plus existing 24h data). ~30 min.
6. **Honesty footer check** — confirm every response has the `assumptions` block populated and the uncertainty band is visible. Non-negotiable.

**Total:** ~3 hrs realistic. Stretch goals if time: tool-caching detection (§8 #4), per-model breakdown chart-ready format.

---

## 12. What we explicitly are NOT doing today

- No Electricity Maps / WattTime integration → static grid intensity for now
- No embodied training emissions → Phase 3
- No model-card per-model real measurements → using published coefficients
- No PAP UI work → just the API, PAP team consumes when ready
- No SDK changes → all data already flows

---

## 13. Open questions for Hena's review

1. **Endpoint shape — Option A or B (§6)?** I lean B for the hierarchical drill-down, but Sharad's PAP contract may prefer flat `/v1/metrics` extension.
2. **Per-token coefficients — accept literature defaults?** Or do we want to commission per-model micro-benchmarks before reporting? (Defaults give us a deliverable today; benchmarks could come Tue+.)
3. **Uncertainty band — ±40% honest, or do we want to soften?** Recommend keeping ±40%. Tier 3 credibility depends on not overstating precision.
4. **Functional unit — "completed task" via root trace_id?** Alternative: "user-resolved goal" (multi-turn). Trace_id is what we can implement today; multi-turn goal requires session-level grouping which the SDK doesn't currently emit as a single ID.
5. **Reduction recommendations — surface even when noisy?** They're heuristics, not proofs. I lean yes with confidence labels (high/medium/low) so consumers see we're suggesting, not asserting.

---

## 14. Out-of-scope but worth flagging to architecture team

- **Marginal vs. average grid intensity** is where most carbon reports lie (per research note). Phase 2 will require an outbound call to Electricity Maps or WattTime — that's a new dependency, new key, new failure mode. Worth flagging to Sharad/architects so it lands in the system diagram, not as a surprise.
- **Embodied training emissions** is an ethics-of-disclosure question as much as a math question. Some providers (Anthropic, OpenAI) publish training-energy figures; some don't. Tier 3 should be honest when we have to estimate — flag the gap.
- **Client-device energy** is genuinely out of scope for us (we don't run on customer devices), but if PAP wants to surface it, the customer app would need to instrument the browser/mobile side. Worth a sentence in the assurance doc to preempt the question.

---

## Appendix A — sample worked example

LoanWise task: user asks "Can I qualify for a $50k loan?"

| Span | Type | Model | prompt_tok | completion_tok | duration | emissions_g |
|---|---|---|---|---|---|---|
| router | LLM | llama-3.1-70b | 1,200 | 80 | — | (1200×0.15 + 80×0.45) × 0.4 × 1.2 / 1000 = 0.10 |
| intl_wire_agent | LLM | llama-3.1-70b | 2,400 | 250 | — | (2400×0.15 + 250×0.45) × 0.4 × 1.2 / 1000 = 0.23 |
| credit_check_tool | tool | (external API) | — | — | 1.2s | (1.2/3.6e12)×3.6e9 × 50 × 0.4 × 1.2 / 1000 ≈ 0.00 (tool emissions tiny vs. inference) |
| reflection | LLM | llama-3.1-8b | 600 | 40 | — | (600×0.04 + 40×0.12) × 0.4 × 1.2 / 1000 = 0.01 |
| **Task total** | | | **4,200** | **370** | | **0.34 g CO₂e** ±40% |

For comparison: a Google search ≈ 0.2 g CO₂e (per Google's own disclosure, 2009). So one LoanWise loan-eligibility task ≈ 1.7 Google searches. That's the kind of intuitive anchor the dashboard should give users.

---

**End of plan — ready for Hena's review.**
