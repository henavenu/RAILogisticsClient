# RAI Trust Platform — CLAUDE.md

Project-level instructions and architecture reference for Claude Code.
Keep current as the codebase evolves.

---

## Project Overview

The **RAI Trust Platform** is a multi-service governance stack that wraps
LLM-based applications with real-time policy evaluation, audit, alerting, and
human oversight. The primary demo application is **MyBank** — a banking
assistant instrumented with the RAI Trust SDK.

---

## Fork Strategy

This is the **proto fork** (`rai-trust-platform-prototype` on the
`Neuro-Trust-MVP` branch). As of 2026-06-20 it is **wholesale-aligned with the
read-only intake repo** (`rai-trust-platform`), with exactly one intentional
delta:

- **`assurance/`** — Live Assurance service (Arize-backed) plus its React
  dashboard prototype. Never present in RO; an opt-in capability for proto
  demos.

Plus two file-level deltas:

- **`cloudbuild.yaml`** — proto's GCP project info (deployment target differs from RO).
- **`.gitignore`** — adds `.launch-logs/`, runtime-noise rules.

Future RO updates should be takeable wholesale. The selective-merge / two-section
`.env.example` pattern from earlier proto cycles has been retired.

---

## Service Architecture

Seven services launched by `start_all.bat`, all on localhost:

| Port | Service | Directory | Notes |
|------|---------|-----------|-------|
| `3001` | **Guardrail Service** | `guardrail-service/` | Content evaluation engine; private (PE-only). Providers: mock (dev), Azure Content Safety, AWS Bedrock |
| `3002` | **PAP** (Policy Admin + Dashboard) | `pap/` | Admin UI + full dashboard. Main entry point for operators |
| `3003` | **Policy Engine** | `policy-engine/` | Public-facing; all SDK-instrumented apps call this |
| `3010` | **Mock Assurance Provider** | `pap/tools/proxy_assurance/` | Dev mock for quality metrics. Launched when `ASSURANCE_URL=:3010` |
| `3012` | **Sentinel Agent** | `trust-agents/sentinel/` | Cross-turn LLM-based audit; receives PE webhooks and sync eval calls |
| `3013` | **Coordinator Agent** | `trust-agents/coordinator/` | Escalation recommendations from Sentinel ALERTs |
| `5000` | **myBank** (demo app) | `sdk/examples/mybank_clean/` | Separate venv; `start_all.bat` starts it last |

**Shared database**: `pap/config/pap.db` (SQLite), materialized from the
committed snapshot `pap/config/pap.seed.db` on every boot by `tools/seed_local.py`.

**Optional**: `assurance/` subdir (port 8000) — Live Assurance with Arize backend.
Not launched by `start_all.bat` by default. To enable, set `ASSURANCE_URL=:8000`
in root `.env` and run `assurance/start_assurance.bat` manually.

---

## Key Design Principles

- **Enforcement modes**: `RAISE` (GovernanceBlock exception → rich UI) / `REDACT` (safe substitution) / `NONE` (audit only). Per-app via `TRUST_ENFORCEMENT_MODE`.
- **Fail mode**: PE returns `PERMISSIVE` on Guardrail unreachable in dev (`FAIL_MODE=open`); `COERCIVE` in prod (`closed`).
- **Snapshot-first config**: GS and PE read runtime config from `config_service_configs` blobs in `pap.db`, populated from `pap.seed.db`. No YAML files at runtime.
- **No localhost fallback**: PAP, Sentinel, and Coordinator refuse to start if their peer-service URL env vars are unset (intentional — forces explicit config in cloud).
- **Tier 2 metrics**: default Mock Assurance Provider on port 3010 returns deterministic quality numbers; flip to Live Assurance only when needed.

---

## Configuration

### Required env vars per service (read from each service's `.env`)

| Service | Required (no fallback) |
|---|---|
| PAP | `POLICY_ENGINE_URL`, `GUARDRAIL_SERVICE_URL` |
| Sentinel | `SENTINEL_PE_URL` |
| Coordinator | `COORDINATOR_PAP_URL`, `COORDINATOR_SENTINEL_URL` |
| GS | `PAP_DB_PATH` (or `AUDIT_SQLITE_PATH` as fallback) |
| Mybank | `TRUST_PE_URL`, `TRUST_APP_ID`, `PAP_URL`, `SENTINEL_URL` |

If a fresh clone's `.env` is missing any of these, the service exits with a clear
`FATAL: required configuration missing` and names the variable.

### LLM provider priority (Mybank, Sentinel, Coordinator)

1. `LLM_DISABLED=true` → stub model (no external calls)
2. Vertex AI (`GOOGLE_CLOUD_PROJECT`)
3. Azure OpenAI (`AZURE_OPENAI_*`)
4. Groq (`GROQ_API_KEY`)
5. Anthropic (`ANTHROPIC_API_KEY`) — Sentinel/Coordinator only
6. Ollama (local) — fallback
7. Heuristic (no LLM)

---

## MyBank Demo Application

LangGraph StateGraph with 8 nodes. Sentinel + Coordinator instrumentation via
the `rai-sdk-langgraph` package (editable-installed at first run).

### Showcase Scenarios

| Endpoint | Scenario | Expected verdict |
|----------|----------|-----------------|
| `POST /api/chat` | Quick Actions (balance/transactions/transfer/wire/profile) | PERMISSIVE |
| `POST /api/chat` | Card Recommendations (travel card, 40+ countries, miles) | HITL via Sentinel A2A sync |
| `POST /api/chat` | Agent Boundary Excursion ($25k overseas, fastest channel) | COERCIVE |
| `POST /api/demo/sanctions_wire` | Russia IBAN/SWIFT wire | COERCIVE |
| `POST /api/demo/structuring_wire` ×3 | Cumulative domestic > $8,500 | PERMISSIVE × 2, COERCIVE × 3rd |
| `POST /api/chat` | Card-Denial / Fraud Review Authority ("why was my $12k declined") | HITL (`hitl_type=council`) — 4 reviewers vote 2-2, two-stage HITL |

---

## Known Limitations / Next Iteration

1. **`requirements.txt` in mybank includes `langchain-google-vertexai`** which has deeply nested module paths that exceed Windows' 260-char path limit when the repo lives under OneDrive. Local Groq dev works; cloud Vertex install fails on Windows + OneDrive. **Fix**: move to `pyproject.toml` with a `[cloud]` optional-dependencies group, install locally with `pip install -e .` and cloud-build with `pip install -e ".[cloud]"`.

2. **Sentinel and coordinator have unused service-level `requirements.txt`** files. They share the root proto venv; their service-level requirements files are never installed by any bootstrap path. Same `pyproject.toml` migration would consolidate these into a single root file.

3. **Tier 2 panel on `index.html`** uses mock provider in this fork. Earlier proto carried a 96-line Assurance-bridge in `pap/api/tier2.py` that short-circuited to live-Arize cache; that bridge is gone with the wholesale-RO take. The cockpit's Tier 2 view uses `/v1/assurance/metrics` (via `pap/api/assurance/service.py`) and is unaffected.

4. **Live Assurance launch is manual**. The `start_all.bat` taken from RO has no launch block for `assurance/`. Run `assurance/start_assurance.bat` separately if you want port 8000 live.

---

## Environment / Configuration Files

- Service-level `.env` files: `pap/.env`, `policy-engine/.env`, `guardrail-service/.env`, `trust-agents/sentinel/.env`, `trust-agents/coordinator/.env`, `sdk/examples/mybank_clean/.env`
- Each has a committed `.env.example` template. `start_all.bat` bootstrap-copies the example to `.env` only if `.env` doesn't exist.
- **Caveat**: when a new key is added to `.env.example`, existing local `.env` files are NOT updated automatically. Manually merge new required keys after RO updates.

---

## What Not to Touch Without Coordination

- **`pap/ui/dashboard.html` Eval Trail interaction graph** (`_ig*` functions, `#eval-interact-view`) — under active rework.
- **`pap/alembic/versions/`** — migrations are immutable history; never edit existing files.
- **`guardrail-service/providers/mock_provider.py` keyword lists** — changes affect all governance demo scenarios; test all MyBank menu options after any edit.

---

## Git / Push Notes

- Branch: `Neuro-Trust-MVP`
- Push tags separately: `git push origin <tagname>` (not included in `git push` by default)
- `pap/config/pap.db` is gitignored (runtime state); `pap/config/pap.seed.db` IS tracked (config snapshot).
- `.launch-logs/`, all `venv/`, `.env`, runtime `*.err`/`*.log`/`traces.jsonl` are gitignored.
