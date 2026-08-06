# Monorepo `assurance/` — Gap Analysis & Re-sync Plan

**Date:** 2026-05-26
**Author:** Hena (RAI Assurance)
**For:** Sharad + Varun (cloud deployment)
**Status:** Diagnostic — friendly heads-up, easy fix path below.

---

## TL;DR

The Tier 2 evals not populating on the deployed cloud link is almost certainly because the merge of `rai-assurance` into the new monorepo's `/assurance/` directory **picked up the API layer but not the underlying eval / metrics / provider packages it depends on**. Looks like the merge swept in `api/` + `core/agents/` and stopped — the rest of the tree (`evals/`, `metrics/`, `core/providers/`, etc.) didn't follow. The service can't import its own modules, so it can't compute the Tier 2 metrics. Easy to complete — proposed fix path at the bottom.

---

## Background — where the Tier 2 code lives

Through April–May 2026 the canonical home for all Tier 2 / observability / assurance work has been Hena's repo:

- **Source of truth:** `https://github.com/henavenu/RAI-Trust-Platform`
- **Active branch:** `integration/pap-fast-evals`
- **Latest commit on that branch (today):** `91b8dd9` — port/comment refresh

This is the repo Sharad and Hena integrated through three rounds:

| Milestone | What we validated |
|---|---|
| **2026-05-14** — first full demo to leadership | End-to-end pipeline: MyBank → SDK → PE + Privacy Proxy → OTel → Arize → our `/v1/metrics` → PAP dashboard. 500-msg overnight batch, 1,326 PE evaluations clean. |
| **2026-05-20** — v1.7 + port reassignment | Sharad's v1.7 drop + canonical port map (Assurance 3010 mock / 8000 live, PE 3003, MyBank 5000) integrated. LLM-evals cache endpoint shipped. |
| **2026-05-21** — LLM-judge calibrated end-to-end | Anthropic Haiku 4.5 as judge LLM, first calibrated run on both MyBank and LoanWise, hallucination/groundedness tagged experimental. |

So the code on `RAI-Trust-Platform/integration/pap-fast-evals` is the **tested, integrated, working** Tier 2 implementation. That's the body of work that needs to land in the monorepo's `/assurance/` directory.

---

## The two repos, side-by-side

| | Hena's repo (T2 source) | Sharad's monorepo |
|---|---|---|
| **URL** | `https://github.com/henavenu/RAI-Trust-Platform` | `https://github.com/sharadcognizant/rai-trust-platform-prototype` |
| **Branch** | `integration/pap-fast-evals` | `master` (default `main` is empty — heads-up below) |
| **Role** | Canonical Tier 2 / Assurance Service | Containerised monorepo for cloud deploy |
| **Head commit** | `91b8dd9` 2026-05-26 | `e631fa1` 2026-05-22 "Sentinel Coordination merge" |

A small adjacent observation: on the monorepo, GitHub's default branch is `main` but `main` only has the initial commit + `.gitignore`. All the real work — including the partial assurance merge — is on `master`. Anyone cloning fresh (e.g. Varun's CI / container build) will get an empty tree unless they explicitly check out `master`. Worth either renaming `master` → `main` or pointing the deploy at `master` explicitly.

---

## Expected vs Actual — what's in monorepo `/assurance/`

**Expected** (mirror of `RAI-Trust-Platform`):

```
assurance/
├── api/              ← FastAPI app + /v1 router
│   └── v1/
│       ├── router.py
│       └── models.py         ← Pydantic response models
├── core/
│   ├── agents/               ← bias-detection agent registry + base
│   ├── models/               ← shared data types
│   ├── providers/            ← Arize provider (span fetch + SDK pairing)
│   ├── store/                ← provider registry
│   └── provider_registry.py
├── evals/                    ← Tier 2 eval logic
│   ├── evals.py              ← Phoenix LLM-as-judge orchestration
│   └── fast_evals.py         ← Detoxify, Presidio, MS-MARCO, DeBERTa NLI
├── metrics/
│   └── tier2metrics.py       ← run_cycle — the 15-metric Tier 2 calculator
├── scripts/                  ← batch testers, seeders, smoke tests
├── dashboard/                ← Streamlit (legacy)
├── dashboard-react/          ← React dashboard
├── deploy/                   ← containerization files
├── docs/
└── tests/
```

**Actual** (in the monorepo, on `master`):

```
assurance/
├── api/
│   ├── main.py              ✅ present
│   └── v1/
│       └── router.py        ✅ present
│       (models.py)          ❌ missing — router.py:14 imports it
├── core/
│   └── agents/
│       ├── base.py          ✅
│       └── registry.py      ✅
│       (span_adapter.py)    ❌ missing — main.py:289 imports it
│   (models/, providers/, store/, provider_registry.py)  ❌ all missing
├── docs/                    ✅ but contents are early Day-1 scaffold
├── tests/                   ✅ but slim
├── pyproject.toml
├── README.md
└── tracing.py
```

So the API layer + agent skeleton came across, but the packages they *import from* didn't follow.

---

## Gap A — Missing Python packages (these cause boot-time `ImportError`)

The merged `api/main.py` and `api/v1/router.py` were carried over largely as-is, which means they still import:

| Module the code expects | Where it's used (line in monorepo) | Status |
|---|---|---|
| `metrics.tier2metrics` (`run_cycle`) | `api/main.py:23`, `api/v1/router.py:155` | ❌ Missing |
| `evals.evals` (`run_llm_evals_window`) | `api/main.py:339`, `api/v1/router.py:271` | ❌ Missing |
| `core.provider_registry` (`registry`) | `api/main.py:290`, `api/v1/router.py:135` | ❌ Missing |
| `core.agents.span_adapter` (`spans_to_records`) | `api/main.py:289` | ❌ Missing |
| `api.v1.models` (`MetricsResponse`, `MetricItem`, `CatalogResponse`, `HealthResponse`) | `api/v1/router.py:14` | ❌ Missing |

If anyone tries `uvicorn assurance.api.main:app` against the current monorepo `master`, it crashes at the first import (line 23). That's the most likely root cause of "T2 evals don't show up on the cloud link" — the service itself probably isn't running, or is running a stub.

---

## Gap B — Peripheral assets that didn't make the merge

Not boot-blocking, but useful for the deploy + demos:

- `scripts/` — `batch_mybank_test.py`, `batch_loanapp_test.py`, `calibrated_1k_corpus.py`, `seed_adversarial.py`, smoke tests
- `dashboard-react/` — the React dashboard (`/llm-evals` route, components)
- `dashboard/` — the Streamlit dashboard (legacy, still useful for quick eyeballing)
- `deploy/` — containerization files we'd been collecting
- `rai_client/` — simulator + use cases
- `data/`, `notebooks/`, `out/` — fixtures + analysis artifacts
- `tests/` — the comprehensive 85-test suite (the monorepo's `assurance/tests/` is slim)
- `prototypes/topology-mockup/` — intentionally gitignored on our side (scratch work), so this one is correctly absent. Not a gap, just calling it out.

---

## Gap C — README in `/assurance/` is the Day-1 scaffold

The README that came across appears to be from the very early scaffold of the project (the Day 1–15 build plan). It references:

- Streamlit as the primary dashboard (we moved to React on Day 12)
- OpenAI as an alternative LLM provider (we've been Anthropic-only since calibration)
- The 1–15 day plan with most rows marked TODO (those are all complete)

Easy fix: replace with the current `RAI-Trust-Platform/README.md` or the integration handoff doc at `docs/integration_for_sharad.md` in our repo.

---

## Gap D — `env.example` at monorepo root is missing key vars

The current `env.example` at the monorepo root covers the guardrail / Sentinel / Coordinator URLs nicely, but is missing the variables the Assurance Service actually reads at startup:

- `ANTHROPIC_API_KEY` — required for Phoenix LLM-as-judge (the 10 LLM-judge metrics)
- `ARIZE_API_KEY` and `ARIZE_SPACE_ID` — required to fetch spans for any metric
- `OTEL_RESOURCE_ATTRIBUTES=model_id=<app_id>` — guidance for client apps so spans land in the right Arize project (the 1:1 mapping we locked in with you on 2026-05-14)
- `EVAL_FAST_MODE` — toggles fast non-LLM path vs LLM-judge
- `JUDGE_MODEL` — `claude-haiku-4-5` default
- `METRICS_USE_LLM_CACHE` — pure-LLM mode for `/v1/metrics`
- `LLM_EVAL_CACHE_ENABLED`, `LLM_EVAL_CACHE_REFRESH_MIN`, `LLM_EVAL_CACHE_APPS`, `LLM_EVAL_CACHE_WINDOWS` — the cache warmer knobs

Without these, even after we fix Gap A, the service will boot but return empty / mocked metrics.

---

## Gap F — `rai_trust_sdk/observability/otel.py` drops `OTEL_RESOURCE_ATTRIBUTES` (Arize-blocking)

**Severity:** High — this silently breaks all OTel emission to Arize.
**File:** `sdk/rai_trust_sdk/observability/otel.py`, line 244.

The current code:

```python
resource = Resource(attributes={SERVICE_NAME: resolved_name})
```

uses the bare `Resource(...)` constructor, which takes the attributes dict literally and discards anything from the `OTEL_RESOURCE_ATTRIBUTES` env var. Because of this, spans emitted by the SDK have `service.name` but **not** `model_id=<app>` — and Arize requires `model_id` (or `arize.project.name`) on every span to associate it with the correct project. Spans without `model_id` get silently rejected.

**Symptom we hit on 2026-05-26:** MyBank with `OTEL_RESOURCE_ATTRIBUTES=model_id=mybank` set in `.env` emitted traffic, MyBank's UI responded correctly, but the Arize project `mybank` showed **0 traces / 0 spans** in the last 15 min — because every span was silently dropped by Arize for missing `model_id`.

**One-line fix:**

```python
# Use Resource.create() (not Resource()) so OTEL_RESOURCE_ATTRIBUTES env vars
# (e.g. model_id=mybank) are merged into the resource.
resource = Resource.create(attributes={SERVICE_NAME: resolved_name})
```

This is the same patch you (Sharad) applied in your earlier prototype tree and that we [[project_progress]] documented in `docs/integration_for_sharad.md` §8 — but it was never pushed upstream, so a fresh clone of the monorepo on 2026-05-26 still has the regression. Worth landing on `master` so the next clone works out of the box.

The patch is applied locally on Hena's machine (in the freshly-cloned `rai-trust-platform-prototype/sdk/...`) to keep today's demo unblocked, but it's not committed or pushed.

---

## Gap E — A few small hygiene items

- `gs.err`, `pe.err`, `pap.err` are committed to `master` — likely leftover from a debug session. Worth `.gitignore`ing the `*.err` pattern.
- Start scripts are `.bat` only (`start_all.bat`, `start_assurance.bat`, etc.) — fine for Windows, but the cloud container build (and Mac dev) needs `.sh` equivalents. We have these in our repo (`scripts/start_all.sh` etc.) — can bring those across.
- Default branch confusion (mentioned above): `main` is empty, `master` has the real code.

---

## Why this matters for the cloud deployment

The container that Varun built / is building was almost certainly built from the monorepo on `master`. Given Gap A, the most likely behaviours on the deployed link are:

1. **Container fails to start** — uvicorn dies at the first import error. The health endpoint never responds.
2. **Container starts but `/v1/metrics` returns 500** — if the imports were swapped for stubs somewhere.
3. **Container starts and returns mock data** — if `MOCK_MODE=true` was left at the env default and the missing-import paths are short-circuited.

Worth checking the container logs for `ModuleNotFoundError: No module named 'metrics'` (or `'evals'`, `'core.provider_registry'`) — that will confirm Gap A in one line.

---

## Recommended fix path — three options, all friendly

We're happy to do any of these; pick whatever's easiest given how you have the monorepo structured:

### Option 1 — Re-merge the full tree from `RAI-Trust-Platform`

The simplest fix. From the monorepo root:

```bash
# from RAI-Trust-Platform integration/pap-fast-evals (commit 91b8dd9)
git remote add rai-assurance https://github.com/henavenu/RAI-Trust-Platform.git
git fetch rai-assurance integration/pap-fast-evals
git read-tree --prefix=assurance/ -u rai-assurance/integration/pap-fast-evals
git commit -m "assurance: pull complete tree from RAI-Trust-Platform integration/pap-fast-evals"
```

This brings the entire repo across as `/assurance/`, overwriting the partial state.

### Option 2 — Git submodule

If you want the monorepo to track our repo without copy-pasting:

```bash
git submodule add -b integration/pap-fast-evals https://github.com/henavenu/RAI-Trust-Platform.git assurance
git commit -m "assurance: track RAI-Trust-Platform as submodule"
```

Cleaner long-term but Varun's container build needs to do `git submodule update --init --recursive`.

### Option 3 — Targeted patch

If you'd rather keep `/assurance/` minimal and add only the missing modules, we can hand you a focused patch covering just:

- `assurance/evals/`
- `assurance/metrics/`
- `assurance/core/providers/`, `core/store/`, `core/models/`, `core/provider_registry.py`
- `assurance/core/agents/span_adapter.py`
- `assurance/api/v1/models.py`

Plus the env vars in Gap D and the `.sh` start script. Smallest diff, but means we'll diverge from `RAI-Trust-Platform` over time.

---

## Sequence we'd suggest

1. **Today / tomorrow** — confirm the diagnosis by checking the cloud container logs for the import error.
2. **This week** — pick Option 1 / 2 / 3 above; we're happy to drive any of them.
3. **Same change** — patch `env.example` with the Anthropic + Arize keys + a comment block pointing to our README for the full var list.
4. **Then** — redeploy the container and confirm Tier 2 evals populate against MyBank traffic (we can run a batch from our side to drive load).

Happy to jump on a call or just async — whichever's faster. The integration we did together in May worked beautifully end-to-end; this is just about getting that same code into the deploy pipeline.

---

## Related context

- `[[project_monorepo_integrated]]` — the original monorepo integration note (2026-05-21)
- `[[project_progress_2026-05-21]]` — last verified-working state of the integrated stack
- `docs/integration_for_sharad.md` (in `RAI-Trust-Platform`) — original handoff doc, still mostly current
