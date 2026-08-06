# RAI Assurance ↔ PAP Integration — handoff notes

**Author:** Hena (Assurance team)
**Date:** 2026-05-13 (initial) / 2026-05-14 (SDK integration) / 2026-05-15 (multi-app validation — LoanApp added)
**Audience:** Sharad
**Status:** Live integration verified end-to-end **for two client apps** — MyBank and LoanApp. Both apps run through identical SDK + policy stack, with isolated Arize projects and segregated metrics surfacing in PAP. See §12 for the LoanApp evidence.

---

## 1. TL;DR

- Your **PAP dashboard** (`localhost:3002/ui`) now successfully reads from our **Assurance API** (`localhost:3010/v1/metrics`).
- All **6 SRS §4.7.3 mandatory metrics** are flowing live with varied values **from real MyBank LangGraph traffic** routed through your SDK.
- The integration follows your `PAP_Assurance_Contract_v0.4` document precisely — including the Gap 5 decision that **threshold/verdict logic lives with PAP, not Assurance**.
- We've added a **fast (non-LLM) evaluation mode** that returns the 6 metrics in **<1s per call** — sustainable for PAP's ~10s polling cadence. The existing LLM-as-judge path remains available via `/v1/llm-evals` for on-demand deep dives.
- **One SDK bug found and patched locally** (see §11) — `Resource(...)` instead of `Resource.create(...)` in `sdk/rai_trust_sdk/observability/otel.py:244` swallowed `OTEL_RESOURCE_ATTRIBUTES`. Worth merging upstream.

---

## 2. Architecture — two paths into the same Arize project

### Today (this validation)
```
scripts/calibrated_1k_corpus.py  →  app SDK  →  OTel  →  Arize  →  /v1/metrics  →  PAP UI
       weighted prompt mix                       (project: rai-assurance)    (fast evaluators)
```

### Production (with your SDK)
```
MyBank LangGraph app
   │
   ├─►  rai-sdk-langgraph  ──►  Policy Engine        (enforcement)
   │
   └─►  PrivacyProxy (strip PII)  ──►  OTel  ──►  Arize  ──►  /v1/metrics  ──►  PAP UI
                                       ↑
                                       configurable via OTEL_EXPORTER_OTLP_ENDPOINT
                                       in MyBank's .env
```

**Key:** Our Assurance service is **source-agnostic**. Any span that lands in the Arize `rai-assurance` project becomes visible to `/v1/metrics`. The MyBank app just needs its SDK pointed at Arize via the standard `OTEL_EXPORTER_OTLP_ENDPOINT` env variable.

---

## 3. What changed tonight (commit-ready)

Four surgical edits to make the dashboard go from "Assurance unreachable" to all-green:

| File | Change | Why |
|---|---|---|
| `api/main.py` | CORS `allow_origins` now includes `localhost:3002` + `3003` | PAP UI runs on 3002; browser was blocking responses |
| `api/v1/router.py` | `_APP_TO_PROJECT = {"mybank": "rai-assurance"}` alias | PAP sends `app_id=mybank`; our Arize project is `rai-assurance` |
| `evals/fast_evals.py` *(new)* | Local non-LLM evaluators for the 6 SRS metrics | LLM-as-judge on free-tier Arize was 30-90s per poll |
| `evals/evals.py` | `EVAL_FAST_MODE=true` short-circuits `run_evals` to the fast path | Production switch — flip back to LLM-judge with one env var |

---

## 4. The 6 SRS metrics — fast-path implementation

All run locally on CPU. First call downloads ~720 MB of models (one-time, cached afterwards). Subsequent calls land at sub-second.

| metric_id | Algorithm | Library | Latency |
|---|---|---|---|
| `toxicity_judge_rate` | BERT toxicity classifier | `detoxify` | ~50ms/span |
| `pii_exposure_rate` | NER + regex on response text | `presidio-analyzer` | ~10ms/span |
| `prompt_injection_rate` | 14 regex patterns (Ignore prev / DAN / jailbreak / etc.) | stdlib `re` | ~5ms/span |
| `response_relevance` | Cross-encoder query-response score | `sentence-transformers` (`ms-marco-MiniLM-L-6-v2`) | ~30ms/span |
| `hallucination_rate` | NLI entailment (response unsupported by user_message) | `sentence-transformers` (`nli-deberta-v3-small`) | ~100ms/span |
| `groundedness_score` | NLI entailment, sentence-level | same model as above | ~150ms/span |

**Validation against LLM-judge:** When tested on the same span set, fast path and LLM-judge agreed on `hallucination_rate=0.5` exactly. Calibration is a tomorrow conversation.

---

## 5. Span attribute compatibility

Our fast evaluators read two attribute forms (in order of preference):

| Reads | Form 1 (custom RAI) | Form 2 (OpenInference / your SDK) |
|---|---|---|
| User input | `rai.user_message` | `input` or `input.value` |
| Model output | `rai.response` | `output` or `output.value` |

Our seeder emits both. Your SDK likely emits OpenInference attributes — that path is supported with no changes needed on either side. **Please confirm what attribute names your `rai-sdk-langgraph` actually sets** so we can lock this in.

---

## 6. Thresholds and verdicts — by your own contract (Gap 5)

The dashboard currently shows green "ok" badges next to all values, including `hallucination_rate = 0.78` which is clearly worth flagging. **This is the contract-compliant behavior.**

From `docs/PAP_Assurance_Contract_v0.4` §1, Gap 5:

> *"SRS §4.7.2 places verdict/threshold logic with PAP, not Assurance. Required change: Remove status and threshold from MetricResult. Return raw value, unit, and (optional) sample_count only."*

Our API correctly returns:
- `value` — raw numeric reading
- `unit` — `%` / `score` / `count` / `ms` per §2.4
- `data_status` — **fetch-quality** signal (`ok` / `no_data` / `provider_unavailable` / `unsupported`), **not** a verdict

**The "ok" badge in PAP UI today reflects `data_status` only.** To color rows by health (red/yellow/green vs threshold), PAP would need to either:

1. Compare `value` against threshold rows in `config_threshold_profiles` (which I saw in your PAP DB seed — 3 profiles), and render the badge based on that comparison, OR
2. Drive the badge color via a new client-side mapping table.

This keeps Assurance stateless and reusable across apps that have different risk tolerances (a fraud-detection app's `toxicity_rate=0.05` is fine; a children's chatbot's might be critical).

---

## 7. The two evaluation modes — when to use which

```
EVAL_FAST_MODE=true   →  /v1/metrics returns 6 SRS metrics in <1s using local models.
                         Live dashboard polling. Default for production.

EVAL_FAST_MODE=false  →  /v1/metrics returns the original Phoenix LLM-as-judge values.
                         30-90s per call. Use for offline / scheduled deep-dives.

/v1/llm-evals         →  Always uses Phoenix LLM-as-judge regardless of mode.
                         Returns 10 metrics including the 4 originals (refusal,
                         correctness, demographic_bias, decision_fairness) plus 6 SRS.
                         Slow. Manual / scheduled use only.
```

---

## 8. Demo evidence captured tonight

1. **Slow path proven** — `/v1/llm-evals?app_id=mybank&...&window=7d` returned `hallucination_rate=0.5, toxicity_judge_rate=0.5, pii_exposure_rate=0.0` with `data_status: ok`. (Screenshot saved.)

2. **Fast path proven** — same 6 metrics from `/v1/metrics?app_id=mybank&...&window=1h` after seeding adversarial spans:

   ```
   hallucination_rate     0.78
   groundedness_score     0.003
   response_relevance     0.264
   toxicity_judge_rate    0.09
   prompt_injection_rate  0.13
   pii_exposure_rate      0.17
   ```

3. **PAP dashboard rendering live values** — `T2 · EVAL QUALITY` panel populated with all 6 metric rows, sourcing from `http://127.0.0.1:3010`. (Screenshot saved.)

4. **Adversarial-detector validation** — synthetic input set with intentional toxic/PII/injection/hallucination spans correctly fired each detector (1/8 toxic, 2/8 injection, 4/8 PII, etc.).

---

## 9. Open items for tomorrow's conversation

1. **Span attributes** — confirm `rai-sdk-langgraph` emits `input.value` / `output.value` (OpenInference) or your own `rai.*` names.
2. **OTel endpoint** — agree which Arize space + project MyBank will ship to.
3. **PrivacyProxy** — confirm the redaction happens *before* OTel emission so Arize never receives raw PII.
4. **PAP threshold layer** — do you want to wire color-by-threshold in PAP UI? (Per your Gap 5, this is PAP's responsibility.) If yes, your `config_threshold_profiles` table is the natural home.
5. **Calibration** — `hallucination_rate=0.78` is aggressive on short conversational pairs. Should we tune toward enterprise-Arize data once we have it?

---

## 10. Quick reference — running the stack locally

```bash
# Terminal 1 — Assurance API (our service)
cd /path/to/rai-assurance
source .venv/bin/activate
uvicorn api.main:app --host 127.0.0.1 --port 3010 --reload

# Terminals 2-4 — your existing prototype services
cd /path/to/rai-trust-platform-prototype
source venv/bin/activate
# Then in three terminals (one per service):
cd guardrail-service && python main.py
cd policy-engine    && python main.py
cd pap              && python main.py

# Verify
curl -s "http://127.0.0.1:3010/v1/metrics?app_id=mybank&window=1h" | python3 -m json.tool

# Browse the live dashboard
open http://localhost:3002/ui
```

To re-seed Arize with test traffic (any time the live project goes empty):

```bash
python scripts/calibrated_1k_corpus.py --app mybank --url http://127.0.0.1:5000 --count 12
# Wait 60-90 sec for Arize ingestion, then re-query.
```

---

## 11. SDK integration — what we did + a bug to merge upstream

### Setup we performed

1. Cloned the prototype repo and ran your `start_all.bat` equivalents on macOS (translated to bash, services on 3001/3002/3003 + our Assurance on 3010 per the post-26d9ba0 port reassignment).
2. Inside `sdk/examples/mybank_clean`:
   - Created a dedicated venv with Python 3.12 (the shared prototype venv was 3.9; `rai-sdk-langgraph` requires ≥3.10).
   - Installed `-e ../sdk`, `-e ../../../privacy-proxy`, `-e ../../../sdk-langgraph`, plus `requirements.txt`.
   - Configured `.env` with Arize OTLP endpoint, headers, and Groq key. Notably added `OTEL_RESOURCE_ATTRIBUTES=model_id=mybank,arize.project.name=mybank` so Arize routes MyBank spans into a per-app project.
3. Ran `uvicorn server:app --host 127.0.0.1 --port 8001` and exercised the chat UI with 3-4 messages (sanctions transfer attempt, account balance, transfer with routing/account, profile query).

### What we verified live

| Layer | Evidence in logs / dashboard |
|---|---|
| SDK → Policy Engine | `GovernedGraph [mybank]: registered 7 nodes with PAP (HTTP 200)` |
| Sanctions enforcement | UI showed *"Transaction blocked — reference to a comprehensively sanctioned jurisdiction detected"* (Russia/SWIFT) |
| Data-leakage enforcement | `COERCIVE verdict — role=llm_response enforcement=REDACT violations=['data_leakage: critical violation detected']` |
| Privacy Proxy on input | `PrivacyProxy — PII masked: detections=['account_num','routing_num']` |
| Privacy Proxy on output | `PrivacyProxy — LLM response PII masked: detections=['email','phone']` |
| Spans → Arize | `mybank` project receiving `rai.guardrail.*`, `rai.agent.*`, `rai.tool.*`, `rai.governance.turn` spans |
| Our `/v1/metrics` consuming SDK spans | `response_relevance=0.997`, `pii_exposure_rate=0` (proxy worked!), `toxicity_judge_rate=0`, `prompt_injection_rate=0` |
| PAP dashboard rendering live values | T2 EVAL QUALITY panel + APP TRUST 82% + violations 35/2/2 + token cost 80 µ$ + Gov Latency p95 21ms |

### Changes on our side to consume SDK spans

The SDK emits paired guardrail spans (`rai.guardrail.user_prompt` + `rai.guardrail.llm_response`) linked by `trace_id` — not the legacy single `support_chat` span. We updated `core/providers/arize_provider.py` to **pair these by trace_id and synthesize a `support_chat` span** with `rai.user_message` + `rai.response` derived from each pair's `input.value`. Legacy `support_chat` spans (e.g. our seeder) still work alongside SDK spans.

We also **removed the `mybank → rai-assurance` app_id alias** in `api/v1/router.py` because the SDK now ships MyBank spans to a dedicated Arize project (`model_id=mybank`). `app_id` and Arize `model_id` are 1:1 now.

### 🐛 Upstream bug — `sdk/rai_trust_sdk/observability/otel.py` line 244

```python
# Before (current)
resource = Resource(attributes={SERVICE_NAME: resolved_name})  # ❌ ignores OTEL_RESOURCE_ATTRIBUTES

# After (our local fix)
resource = Resource.create(attributes={SERVICE_NAME: resolved_name})  # ✅ merges env attrs
```

`Resource.create()` is the standard OTel API that merges the explicit attribute dict with everything in `OTEL_RESOURCE_ATTRIBUTES`. With the old `Resource(...)` constructor, Arize rejected every span with `"model_id span resource attribute or arize.project.name span attribute is required"` because the env attributes never reached the exporter.

We have this fix patched locally (editable install). Recommend you merge it upstream — your `setup_otel` docstring even references `OTEL_RESOURCE_ATTRIBUTES` as a supported config surface.

### Architecture decision — 1:1 app-to-Arize-project (locked)

Each onboarded application gets its own Arize project. `app_id` and Arize `model_id` are 1:1 with no aliasing layer:

```
app_id=mybank   →  Arize project "mybank"
app_id=loanapp  →  Arize project "loanapp"        (future apps onboard the same way)
```

This matches your contract §2.5 ("scoped by app_id"), gives clean per-app isolation in Arize, and lets each application enforce its own data-retention / access controls. Implemented by setting `OTEL_RESOURCE_ATTRIBUTES=model_id=<app_id>` in each app's `.env`.

### One open question for you

The dashboard's `STATUS` badge currently renders `data_status` (data quality) instead of value-vs-threshold verdict. Per your Gap 5, that verdict layer belongs in PAP. Want to wire `config_threshold_profiles` to the badge color in a future iteration?

---

## 12. Multi-app validation — LoanApp onboarded (2026-05-15)

Per your ask on 2026-05-14 (keep LoanApp separate from MyBank, use it to exercise the PAP "register new app" UI), here's the evidence that **a second client app was onboarded today using only the platform — no platform-side code changes**.

### 12.1 Where the new app lives

We introduced a new sibling directory for client apps, **outside the platform repos**:

```
RAI May manifestation/
├── rai-assurance/                  (this repo — our APIs)
├── rai-trust-platform-prototype/   (your stack)
└── rai-clients/                    ⭐ NEW — customer apps go here
    └── loanapp/                    Green-themed LoanApp
```

**Why this structure:** customer apps shouldn't live inside the platform repo (`rai-trust-platform-prototype/sdk/examples/`) — that pattern blurs the platform↔client boundary and doesn't reflect how real customers will integrate. Each client now sits as a standalone project that depends on the SDK as a package.

MyBank still runs from its current location in your prototype for continuity; we'll mirror it into `rai-clients/` in a later pass.

### 12.2 Visual differentiation — the LoanApp green theme

To make the multi-tenant story tangible in demos, LoanApp ships a distinct visual identity (the same UI shell as MyBank but reskinned):

| | MyBank | LoanApp |
|---|---|---|
| Header | Navy `#0f2744` | Forest `#14532d` |
| Accent | Sky blue `#4fc3f7` | Lime `#4ade80` |
| Background | Gray `#eef1f6` | Mint `#f0fdf4` |
| Wordmark | My**Bank** | Loan**App** |
| Welcome icon | 🏦 | 🌱 |
| Avatar | MB | LA |
| Display name in PAP | Mybank | LoanWise |

Visitors can immediately see two different "customer brands" sharing the same trust platform — useful when explaining the multi-tenancy story to non-technical stakeholders.

### 12.3 SDK instrumentation — the headline finding

**Zero LoanApp-specific SDK code was written.** The entire "instrumentation" for the new client was two environment variable changes:

```diff
# rai-clients/loanapp/.env
- TRUST_APP_ID=mybank
+ TRUST_APP_ID=loanapp

- OTEL_RESOURCE_ATTRIBUTES=model_id=mybank,arize.project.name=mybank,...
+ OTEL_RESOURCE_ATTRIBUTES=model_id=loanapp,arize.project.name=loanapp,...
```

Everything else — Policy Engine integration, Privacy Proxy, OTel exporter, A2A evaluation — is provided by the SDK's single `instrument(banking_graph, ...)` call in `server.py:52`. This is the strongest validation of the SDK's plug-and-play story so far: **a customer can onboard with one import and one function call.**

### 12.4 PAP UI onboarding — the actual flow tested

We deliberately avoided seed scripts and used the **App Stack tab** in the PAP UI (`http://localhost:3002/ui` → App Stack → Register New App) to validate the real customer-onboarding path. Configuration applied:

| Field | Value | Mirrors MyBank? |
|---|---|---|
| App ID | `loanapp` | distinct |
| Display name | `LoanWise` | distinct |
| Base tier | `MEDIUM` | ✅ |
| Enforcement mode | `BLOCK` | ✅ |
| Tier 1 SYSTEM pack | `mandatory` (auto) | ✅ |
| Tier 1 PLATFORM pack | `session_guard` | ✅ |
| Tier 2 DOMAIN pack | `sanctions` | ✅ |
| Active | yes | ✅ |

DB verification (`pap/config/rai_trust.db`) post-onboarding:
```
app_id   pack_id        active
-------  -------------  ------
loanapp  sanctions      1
loanapp  session_guard  1
mybank   sanctions      1
mybank   session_guard  1
```
Apples-to-apples — any divergence in metrics between the two apps is now attributable to traffic, not policy configuration.

### 12.5 Traffic + governance evidence

A 50-message functional batch (`scripts/batch_loanapp_test.py`, 5-second interval, mixed categories: benign / injection / PII / sanctions / toxic / AML / off-topic) was executed against LoanApp on port 8002. **Zero HTTP errors, full SDK pipeline fired on every turn.**

Selected log excerpts (from `rai-clients/loanapp/server.py` runtime):
```
PrivacyProxy — PII masked before PE evaluation, detections=['account_num', 'routing_num']
PrivacyProxy — LLM response PII masked before PE evaluation, detections=['account_num', 'routing_num']
COERCIVE verdict — role=llm_response enforcement=REDACT violations=['data_leakage: critical violation detected']
Layer A2A (tool-trigger) — evaluating AGENT_TO_AGENT for tool 'international_transfer' → agent 'intl_wire_agent' tokens=1468
```
The sanctions pack triggered a **COERCIVE verdict on data leakage** mid-batch — same enforcement chain that MyBank exercises, now firing on a different app's traffic.

### 12.6 Metric segregation — the dashboard payoff

Both apps' metrics segregate cleanly via `/v1/metrics?app_id=…`:

```bash
curl /v1/metrics?app_id=loanapp&window=1h   →  21 metrics, 13 with live data, data_status=ok
curl /v1/metrics?app_id=mybank&window=24h   →  21 metrics, 13 with live data, data_status=ok
```

Selected LoanApp metric values (computed from today's batch):
| Metric | Value | Unit |
|---|---|---|
| `toxicity_rate` | 0.000 | % |
| `pii_exposure_rate` | 0.133 | % |
| `hallucination_rate` | 0.800 | % |
| `groundedness_score` | 0.016 | score |
| `response_relevance` | 0.528 | score |
| `conversational_latency_p50_ms` | 1108.2 | ms |
| `prompt_injection_rate` | 0.000 | % |
| `approval_rate_disparity_by_gender` | — | `no_data` (deliberate — see §12.7) |

PAP cockpit (`/ui/dashboard`) switches between apps via the APP SCOPE selector with no code change required on either side.

### 12.7 Bias/fairness metrics — the next deliverable

Six fairness metrics (`approval_rate_disparity_by_gender`, `decision_fairness_rate`, `risk_score_disparity_by_age`, etc.) report `no_data` on LoanApp **by design**. They require structured decision events with demographic attributes attached — which will arrive when the **loan-application form** (next deliverable) starts emitting decision spans containing age / gender / income / credit-score / address.

Design is locked: the form posts to `/api/loan-request`, the endpoint synthesizes a governed graph turn (so the SDK still emits spans), and the protected attributes ride along as span attributes. Fairness evaluators in `evals/` already read those attribute names — they'll start populating as soon as traffic arrives. Round-2 work, expected this week.

### 12.8 Files of interest for this validation

| Path | What's in it |
|---|---|
| `rai-clients/loanapp/server.py` | LoanApp FastAPI server + the single `instrument(...)` call that wires the SDK |
| `rai-clients/loanapp/.env` | The two-line config diff that turned MyBank into LoanApp |
| `rai-clients/loanapp/ui/index.html` | Green-themed UI (CSS-only diff from MyBank) |
| `rai-assurance/scripts/batch_loanapp_test.py` | 50-msg loan-themed prompt corpus (this repo) |
| `rai-trust-platform-prototype/pap/config/rai_trust.db` | PAP DB with the loanapp app + pack assignments |
