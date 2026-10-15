# VitaLink Distribution × NeuroTrust: demo guide

VitaLink Distribution is the demo's pharma distributor (US → EU). On the platform its app id is
`pharmalogistics`, and the code lives in the `RAILogisticsClient` repo.

## 1. Git repo

| | |
|---|---|
| Repo | https://github.com/henavenu/RAILogisticsClient |
| Branch | **`main`** (the standalone client for the beta Trust platform). The old full-platform version is tagged `old-platform`. |
| Clone | `git clone https://github.com/henavenu/RAILogisticsClient.git PharmaLogistics-Demo` |
| What's in it | The app (`server.py`, `graph.py`, `tools.py`, `mock_data.py`, `council_reviewers.py`), agent passports (`passports.py`, `ui/passports.html`), registry cards (`agent_cards.json`), onboarding (`onboard_trust.py`), smoke test (`scenario_check.py`), launcher (`run.sh`), local Trust gateway (`trust_gateway.py`), the SDK wheels (`sdk-dist/wheelhouse`, RAI Trust SDK 0.1.2), and the docs (`docs/`). |
| Not in git | `.env` (LLM key, `RAI_APP_KEY`) and `rai.env` (Trust URLs). `run.sh` creates both from the `.example` files. |

## 2. Integration with NeuroTrust

The app contains no platform code. It is a client of the Trust APIs over HTTP.

1. **SDK.** `run.sh` installs `rai-sdk-langgraph` from `sdk-dist/wheelhouse`. `server.py` calls `instrument(graph, app_id="pharmalogistics", app_key=…)`, and from then on the Policy Engine evaluates every agent, tool and agent-to-agent call before it runs.
2. **Where the platform is.** `rai.env` holds the Trust URLs:
   - `RAI_PE_URL=http://127.0.0.1:3003` (Policy Engine)
   - `RAI_PAP_URL=http://127.0.0.1:3002` (PAP)
   - `RAI_EVAL_MODE=SYNC`, `RAI_ENFORCEMENT_MODE=RAISE` and `RAI_TIMEOUT=30`, which make it an enforcing gate.

   Pointing at GCP later means changing these two URLs plus `RAI_APP_KEY`.
3. **Onboarding (config only).** Run `venv/bin/python onboard_trust.py` once. It is idempotent, and the platform side is configured only through the Trust APIs. It does the following:
   - Registers the app and writes its credential to `.env` (`RAI_APP_KEY`).
   - Assigns the policy packs (session_guard, sanctions, prompt_filters, eu_ai_act, agent_coordination, sanctions_advanced, agent_passport).
   - Creates the `passport_oversight` rule and an app-scoped `hitl_trigger` policy, then publishes.
   - Pushes the 14 agent cards into the registry. Start the app once before this step, so the agents exist.
4. **How a passport is enforced.** Before a governed action, the app stamps the agent's passport onto the call (`passport_jurisdiction: EU · passport_oversight: HITL · passport_tier: T4`). The platform's rule reads the stamp and returns WARN plus **REQUIRE_HUMAN_REVIEW**, so the SDK pauses the agent. After approval, the resume is stamped `QP-CERTIFIED` and passes.
5. **Human Review path.** The pause appears in PAP's HITL queue (`/v1/hitl`). The app polls `/api/hitl/status/<session>` and resumes when a reviewer approves.

## 3. Demo requirements

| Need | How |
|---|---|
| Mac with Python ≥ 3.10 | `brew install python@3.12` if needed |
| NeuroTrust beta running | `cd ~/NeuroAITrust-beta && ./start_all.sh`. This starts Postgres :5432, Redis :6379, Guardrail :3001, PAP :3002, PE :3003, Dashboard :3004, Sentinel :3012 and Coordinator :3013. |
| **Local gateway on :3080** | Started for you by `./run.sh` whenever `rai.env` points at a local stack. The log line `Human Review: http://127.0.0.1:3080/ui/dashboard?goto=hitl` confirms it; a WARNING means it isn't answering. It gives the dashboard's Human Review tab the routing the GKE ingress provides. **Locally, always use the :3080 dashboard. :3004 shows an empty Human Review.** |
| LLM key | `GROQ_API_KEY` in `.env` |
| App onboarded | `venv/bin/python onboard_trust.py` (first time, or after a platform DB reset) |
| App running | `cd ~/PharmaLogistics-Demo && ./run.sh`. The log should say `Ready -- governance ACTIVE … enforce`. |
| Smoke test | `venv/bin/python scenario_check.py` → `ALL PASS` (12 scenarios, ~2 min) |
| Tabs to open | App http://127.0.0.1:5001 (sign in `dana.reyes` / `1234`) · Passports http://127.0.0.1:5001/passports · Dashboard http://127.0.0.1:3080/ui/dashboard |
| Clean slate | Click **Reset demo** in the app before you start. |

**On a held request, the app shows an "Open Human Review ↗" link** that opens the dashboard on the Human Review tab. Use it instead of typing a URL.

**GCP (final demo):** in `rai.env`, set `RAI_PE_URL` / `RAI_PAP_URL` to the GCP endpoints. In `.env`, set `RAI_APP_KEY` (from `onboard_trust.py --pap … --pe …` against GCP) and `TRUST_DASHBOARD_URL=https://<gcp-host>/ui/dashboard`. The gateway is skipped, because the GKE ingress already routes Human Review.

Labels: the app shows **PASS / WARN / BLOCK / REVIEW**. The NeuroTrust dashboard shows the platform names for the same verdicts: PERMISSIVE / NORMATIVE / COERCIVE / HITL.

## 4. Demo scenarios

**1. Agent passports and risk tiers.** Open **/passports**. Each of the 14 agents has a passport with a primer risk tier (agency, impact and reach, plus amplifiers). Click **Cross-Border Logistics**: crossing a jurisdiction sets it to Tier 4, so the passport is valid for 6 months. The drawer shows two jurisdiction stamps: US *Execute · HOTL* and EU *Assist · HITL, Qualified Person certifies*. It also shows the policy gap, data sovereignty and the chain of delegated authority.

**2. US release: the agent acts.** In the app, click **🇺🇸 Release · US**. The US stamp allows Execute with a human on the loop, so the release goes through with a reference ID. The activity panel shows **PASS** for Cross-Border Logistics. This is the baseline for the next step.

**3. EU release: same agent, same action, held.** Click **🇪🇺 Release · EU**. The reply says EU release needs a Qualified Person, and the request pauses with **REVIEW**. Explain that the app stamped the passport onto the call, and that the platform's `passport_oversight` rule turned it into a human-review hold. No platform code was changed.

**4. The Qualified Person certifies.** Click **Open Human Review ↗** in the held reply (or dashboard → Human Review): the item is pharmalogistics / cross_border_logistics / agent_arbitration. Click **Approve**. Within ~5 seconds the app shows the release completed. The audit trail records `QP-CERTIFIED`, which captures which authority the human used, not just that someone approved.

**5. Embargoed shipment.** Click **Embargoed shipment**: a Russian consignee on the denied-party list. The platform returns **BLOCK** with OFAC/BIS reasons. Passports never override sanctions screening.

**6. Cold-chain boundary excursion.** Use the **Cold-chain excursion** prompt ("expedite … skip the cold-chain validation"). The Route Optimizer tries to act outside its scope and gets **BLOCK** for a scope violation. Agents can't trade product safety for speed, even when a user asks.

**7. Controlled-substance structuring (DEA SOM).** Click **Controlled-substance orders** three times. The results are PASS, PASS, then **BLOCK**: each order is under the threshold, but together they cross 8,500 units. This catches a diversion pattern across orders, the way a regulator would see it.

**8. Diversion Review Board (multi-agent deadlock).** Use the **Diversion Review Board** prompt ("Why was our $12,000 … order held?"). Four reviewer agents with conflicting goals split 2–2 over two rounds, the coordinator can't break the tie, and the platform escalates with **REVIEW**. In Human Review, open the item to show the votes and decide.

**9. Agent registry (close).** Open the dashboard → **Agent Registry** → app *pharmalogistics*. All 14 agents have cards (purpose, authority, allowed/prohibited actions, escalation). Only Cross-Border Logistics is in passport mode, because it crosses a boundary the company doesn't control. Close on what comes next: passports as a platform object, and a third-party carrier agent with its own passport.

### If something misbehaves
| Symptom | Fix |
|---|---|
| Human Review is empty | You're on :3004. Use the app's **Open Human Review ↗** link, or http://127.0.0.1:3080/ui/dashboard. If `run.sh` printed a gateway WARNING, start the platform and re-run `./run.sh`. |
| *pharmalogistics* missing from the Agent Registry dropdown | Restart the app (`./run.sh`). A beta PAP bug retires agents when an app re-registers; the app now registers twice to undo it. |
| EU release isn't held | `venv/bin/python onboard_trust.py`, then retry after **Reset demo** |
| No new item after a repeat run | PAP keeps one item per session. Click **Reset demo** and retry. |
| App log says `DEGRADED` | Re-run `onboard_trust.py` and restart `./run.sh` (beta's seeded Postgres sequences lag). |
| Any scenario is wrong | `venv/bin/python scenario_check.py` shows which one failed |
