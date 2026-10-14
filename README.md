# VitaLink Distribution — standalone Trust client

A multi-agent pharmaceutical supply-chain assistant (LangGraph) governed by the
**RAI Trust platform** through the RAI Trust SDK. This repo is the **app only**: it
installs the SDK from `sdk-dist/wheelhouse` and reaches the Trust APIs over HTTP.
No platform code lives here — point it at the local beta stack or the GCP deployment
by changing URLs.

Every agent carries an **agent passport** (`passports.py`, list at `/passports`), and
the passport is enforced at runtime: the same Cross-Border Logistics agent releasing
the same consignment is autonomous in the US and held for a Qualified Person in the EU.

## Run

```bash
./run.sh                           # first run: venv + deps + SDK wheels; then serves :5001
venv/bin/python onboard_trust.py   # once per Trust platform (idempotent)
venv/bin/python scenario_check.py  # all menu scenarios against the live app
```

| File | Purpose |
|---|---|
| `rai.env` | SDK config: `RAI_PE_URL`, `RAI_PAP_URL`, eval/enforcement mode, timeout (from `rai.env.example`) |
| `.env` | LLM key, `TRUST_APP_ID`, `RAI_APP_KEY` (written by `onboard_trust.py`), URLs the app calls directly |

To move to another Trust platform (e.g. GCP): set the URLs in `rai.env` and `.env`, run
`onboard_trust.py --pap <url> --pe <url>`, restart.

## What `onboard_trust.py` configures (config only, via the Trust APIs)

1. Registers `pharmalogistics` (`POST /v1/apps`, domain `healthcare`) and writes the
   one-time credential to `.env`.
2. Assigns packs: `session_guard`, `sanctions`, `prompt_filters`, `eu_ai_act`,
   `agent_coordination`, `sanctions_advanced`, `agent_passport`.
3. Passport enforcement: declarative rule `passport_oversight` (Policy Builder), the
   app-only `agent_passport` pack, and an app-scoped policy with `hitl_trigger=true`.
4. Publishes, so the Policy Engine reloads.

## Scenarios

| Menu item | Expected |
|---|---|
| Inventory positions · Order activity · Facility profile | PERMISSIVE |
| Replenishment order | PERMISSIVE |
| 🇺🇸 Release · US (Execute) | PERMISSIVE — passport US stamp: Execute · HOTL |
| 🇪🇺 Release · EU (QP confirms) | **HITL** — passport EU stamp: Assist · HITL → Human Review → QP certifies → released |
| Cold-chain excursion (expedite / skip validation) | COERCIVE (scope violation) |
| Embargoed shipment (Russia, denied party) | COERCIVE (OFAC/BIS) |
| SOM structuring ×3 | PERMISSIVE, PERMISSIVE, COERCIVE |
| Diversion Review Board ($12k controlled-substance hold) | HITL (2-2 council deadlock) |
| Sourcing negotiation | **parked** — nodes missing; next to fix |

## Agent passports

Risk tier follows the RAI primer: the highest of **agency**, **impact** and **reach** sets
the tier; each **amplifier** (coordination, trust-boundary exposure) adds one; swarm
behaviour or a **cross-jurisdiction** boundary sets **Tier 4** alone. Oversight follows
agency — Instruct/Assist → HITL, Execute → HOTL, Delegate → HIC — and is stamped **per
jurisdiction**. Validity shortens with tier (T4 6 months … T1 36 months).

**Passport enforcement.** Before the Cross-Border agent's `international_transfer` is
governed, the app stamps the passport for the destination onto the call
(`passport_oversight`, `passport_jurisdiction`, `passport_tier`). The platform's
`passport_oversight` rule scores a `HITL` stamp NORMATIVE, and the app-scoped
`hitl_trigger` policy turns that into `REQUIRE_HUMAN_REVIEW` — the SDK raises
`HITLInterrupt` and the item lands in the platform's Human Review. On approval the app
resumes with `passport_oversight: QP-CERTIFIED` (reviewer recorded), which passes.

The passports live in the app today because the beta platform has no passport model.
The natural platform home (passport columns on agent cards, a PE passport gate, a
passport list in the Agent Registry) is a follow-up to propose to the platform team.
