# RAI Assurance API — SRS §4.7 Remediation Plan

**Date:** 2026-05-11
**Source:** Gap analysis between current `api/main.py` and SRS §4.7 + PAP contract proposal
**Scope:** 8 SRS-defined gaps + 6 net-new contract items

---

## 1. Executive summary

The current Assurance API works as a project-wide observability service. The proposed PAP contract reshapes it into a per-application, batch-friendly metrics surface with explicit no-data signalling, namespaced identifiers, and a discovery endpoint. The work is well-scoped: about a third is additive and ships today, about a third is a refactor that needs the architecture decisions below, and the remainder is new evaluator builds for next sprint.

Six decisions are needed from architecture before the refactor work can start. Listed in §2 below.

---

## 2. Decisions architecture must confirm

These block downstream work; please resolve in the meeting.

| ID | Decision | Options | Recommendation |
|---|---|---|---|
| **D1** | `app_id` ↔ Arize project mapping | (a) 1:1 — app_id is the Arize project name. (b) Many apps per project — filter by a `rai.app_id` span attribute. | **(a) for v1**, (b) only if a single project must host multiple apps in production |
| **D2** | Mandatory vs custom metric strategy | Build all 6 SRS-mandatory metrics with Phoenix LLM-as-judge OR wrap existing internal eval pipelines | Phoenix-as-judge for 5; keep PII as our regex + Phoenix hybrid |
| **D3** | Verdict ownership | (a) Remove `status`/`threshold` from API entirely (PAP owns). (b) Keep as optional advisory fields. | **(a)** — clean separation, matches SRS §4.7.2 |
| **D4** | Versioning | Break existing `/metrics/*` in place OR add `/v1/*` and deprecate old paths | **Add `/v1/*`** alongside; deprecation window of 60 days before removing old paths |
| **D5** | Eval cadence | Inline run on every request (slow, costly) OR scheduled refresh + cache (15-min interval) | **Hybrid:** statistical metrics inline; LLM-as-judge cached with 15-min refresh |
| **D6** | `context_utilisation` (in PAP UI, not in SRS catalog) | PAP drops OR escalate as SRS §4.7.3 amendment | Defer to PAP team; not in our mandatory set |

---

## 3. Tier 1 — Additive, ship today (no architecture sign-off needed) — ✅ COMPLETE

Landed 2026-05-11 as `api/v1/`. Existing `/metrics/*` endpoints continue to serve current consumers unchanged.

| # | Gap | Item | Status |
|---|---|---|---|
| 1 | §2.10 | `/v1/docs`, `/v1/redoc`, `/v1/openapi.json` — interactive API docs | ✅ Done |
| 2 | Gap 8 | `GET /v1/catalog` — discovery endpoint listing supported `metric_id` values | ✅ Done |
| 3 | — | `GET /v1/healthz` — liveness + provider reachability | ✅ Done |
| 4 | Gap 6 | `data_status: no_data` + `value: null` on zero-sample windows (was silently `0.0`) | ✅ Done |
| 5 | net-new | Top-level + per-metric `data_status` enum (`ok` / `no_data` / `provider_unavailable` / `unsupported`) | ✅ Done |
| 6 | Gap 4 | `?ids=a,b,c` batch parameter for fetching named subsets in one call | ✅ Done |
| — | Gap 7 (partial) | Window enum (`1h` / `6h` / `24h` / `7d`) implemented on `/v1/metrics` | ✅ Done (since/until still pending in Tier 2) |
| — | Gap 1 (partial) | `app_id` required query param on `/v1/metrics` and `/v1/metrics/{id}`; pass-through to provider scope | ✅ Done (1:1 mapping per D1 recommendation; revisit if D1 chooses option b) |

**Test coverage:** 11 new tests in `tests/test_v1_api.py`. Full suite green at 62/62.

**Files:** `api/v1/__init__.py`, `api/v1/models.py`, `api/v1/router.py`. Mounted via `app.include_router(v1_router)` in `api/main.py`.

---

## 4. Tier 2 — Refactor, this sprint — ✅ COMPLETE (partial close-outs from Tier 1 + arch-meeting decisions)

Landed 2026-05-11 post architecture meeting. All `MetricResult` shape changes propagated through metrics, evals, legacy `/metrics/*`, dashboard, and tests.

| # | Gap | Item | Status |
|---|---|---|---|
| 7 | Gap 7 | Window enum (`1h` / `6h` / `24h` / `7d`) + `since`/`until` ISO-8601 range with 7-day max + mutual-exclusion validation | ✅ Done |
| 8 | Gap 1 | `app_id` required query param on `/v1/metrics`; `rai_client` now emits `rai.app_id` on every OTel span (env-configurable via `RAI_APP_ID`) | ✅ Done |
| 9 | Gap 2 | Internal `MetricResult.metric_name → metric_id` rename across `tier2metrics.py`, `evals.py`, legacy endpoints, dashboard, and tests | ✅ Done |
| 10 | Gap 5 | `status` + `threshold` stripped from `MetricResult` everywhere; pillar status rollups removed from `/metrics/summary`; Streamlit dashboard now shows raw values only (verdicts are PAP's responsibility) | ✅ Done |
| 11 | net-new | Namespaced metric IDs: `quality.X`, `safety.X`, `privacy.X`, `reliability.X`, `fairness.X` | ❌ Deferred — coordinated rename of all 21 metric IDs to be done as one cutover alongside Tier 3 |
| 12 | net-new | `custom.<app_id>.<metric_name>` namespace for app-specific metrics + scope enforcement | ❌ Deferred — needs no metric currently; revisit when first app-specific metric is added |
| 13 | net-new | Per-metric `error` field, sanitised — no credentials/AQL/provider response leaks | ✅ Done as part of MetricItem shape |

**Test coverage after refactor:** 81/81 green (75 prior + 6 new since/until tests).

---

## 5. Tier 3 — Build new evaluators — ✅ COMPLETE

Landed 2026-05-11. All 6 SRS §4.7.3 mandatory metrics built as Phoenix LLM-as-judge custom classifiers (per D2 — Phoenix for all 5, hybrid for PII).

| `metric_id` | Status | Implementation |
|---|---|---|
| `hallucination_rate` | ✅ Done | Custom Phoenix classifier (`hallucinated` / `grounded`) over support_chat I/O |
| `groundedness_score` | ✅ Done | Custom Phoenix classifier returning *positive* grounded rate; unit=`score` |
| `response_relevance` | ✅ Done | Custom Phoenix classifier returning *positive* relevant rate; unit=`score` |
| `toxicity_judge_rate` | ✅ Done | Custom Phoenix classifier (`toxic` / `safe`); coexists with existing statistical `toxicity_rate` |
| `prompt_injection_rate` | ✅ Done | Custom Phoenix classifier detecting jailbreak / role-override / hidden-instruction attempts |
| `pii_exposure_rate` | ✅ Done | Custom Phoenix classifier for *contextual* PII disclosure (complements regex `pii_detection_rate` — hybrid per D2) |

**Endpoint:** `GET /v1/llm-evals?app_id=X&window=1h` — runs all 10 LLM-as-judge evaluators (4 existing + 6 new) and returns the v1 envelope with per-metric `data_status` + `value`. Slower than `/v1/metrics` (one LLM call per span per evaluator); meant for on-demand deep dives.

**Re-usable pattern for future evaluators:**
1. Define prompt template
2. `@lru_cache` evaluator factory via `_make_classifier(name, prompt, positive, negative)`
3. `judge_X(spans)` per-span judgment helper
4. `X_rate(spans, project, judgments)` aggregation returning `MetricResult`
5. Register in `_CATALOG` (api/v1/router.py) + `_LLM_EVAL_IDS`
6. Wire into `run_llm_evals_window()` for batch execution

**Test coverage:** 12 stub-judgment tests in `tests/test_evals_srs.py` + 4 endpoint tests in `tests/test_v1_api.py`. Full suite 85/85 green.

---

## 6. Tier 4 — Deferred / clarify

| Item | Reason | Owner |
|---|---|---|
| `context_utilisation` metric | Not in SRS §4.7.3. PAP UI references it. | D6 — PAP team |
| Authentication / per-app API key scoping | Implied by `app_id` model, not in current gap analysis | Architecture follow-up |
| Pillar-level status rollups in `/metrics/summary` | Moves to PAP if D3 chooses option (a) | D3 |

---

## 7. Proposed execution sequence

**Today (2026-05-11):**
- Architecture meeting: confirm D1–D6
- Land Tier 1 #1–6 (additive only, no decisions needed)

**Within 2 days:**
- Begin Tier 2 #7–13 once D1, D2, D3, D4 are signed off
- Cut a new `/v1/*` router; keep old endpoints alive in parallel

**Next sprint:**
- Tier 3 evaluator builds — split across team

**60 days post-`/v1` launch:**
- Deprecate and remove old `/metrics/*` endpoints

---

## 8. What's not in scope

To prevent scope creep:

- No new pillars beyond the SRS §4.7.3 set
- No dashboard work (Streamlit + React stay on existing endpoints until `/v1` is stable)
- No changes to the Arize provider SPI itself — only how we call it
- No changes to the LangGraph client apps in `rai_client/`
