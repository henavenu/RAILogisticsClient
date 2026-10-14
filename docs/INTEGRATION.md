# VitaLink Distribution — Pharma Supply Chain — Integration Outline

**Client demo:** Cencora (pharmaceutical distribution & logistics)
**Client app:** *VitaLink* — a supply-chain assistant instrumented with the RAI Trust SDK, cloned from the MyBank reference app.

---

## 1. What we are showing

Cencora runs agentic/LLM workflows across procurement, replenishment, cold-chain
dispatch, and cross-border shipping. The **RAI Trust Platform** wraps those
agents with **real-time policy evaluation, audit, alerting, and human oversight**
— so an AI agent that *drafts a purchase order, routes a shipment, or recommends
a source* is governed the same way a bank transfer is governed in MyBank.

The client app is the *demo surface*. The governance value lives in the platform
services it calls; the app itself only needs the **SDK** wrapped around its
agent/tool calls.

---

## 2. Integration model (how a client app plugs in)

```
┌────────────────────────┐        ┌──────────────────────────────────────────┐
│  Cencora agent app     │        │            RAI Trust Platform              │
│  (LangGraph + tools)   │        │                                            │
│                        │  SDK   │  Policy Engine (3003)  ← public entrypoint │
│  rai-sdk-langgraph  ───┼───────▶│  Guardrail Service (3001) private eval     │
│  wraps every tool /    │  eval  │  Sentinel (3012)  cross-turn LLM audit     │
│  agent-to-agent call   │◀───────│  Coordinator (3013) escalation             │
│                        │verdict │  PAP (3002)  admin + dashboard + HITL       │
└────────────────────────┘        └──────────────────────────────────────────┘
```

**Three integration touch-points — all provided by the SDK:**

1. **Tool-call governance (Layer 2).** Each LangChain/LangGraph tool is evaluated
   *before* it executes. The Policy Engine returns `PERMISSIVE` / `NORMATIVE` /
   `COERCIVE`. Enforcement mode is per-app:
   - `RAISE` → SDK throws `GovernanceBlock` → app renders a rich block card.
   - `REDACT` → SDK substitutes a safe refusal message.
   - `NONE` → audit only (shadow mode — ideal for the first Cencora pilot).

2. **Agent-to-agent (A2A) governance.** When one agent delegates to another
   (e.g. *Route Optimizer → Compliance Reviewer*), the outbound prompt is
   evaluated. Repeated/circular delegation trips a threshold and escalates to
   **Human-in-the-Loop (HITL)** via PAP.

3. **Cross-turn audit (Sentinel).** Sentinel receives PE webhooks and sync eval
   calls and detects patterns *across turns* — e.g. order **structuring** to stay
   under a reporting threshold, or a **boundary excursion** where an agent uses a
   channel outside its authority.

**App-side code footprint is small:** set six env vars, wrap the graph with the
SDK, and catch `GovernanceBlock` / `HITLInterrupt`. No policy logic lives in the
app — policies are authored in PAP and read from the shared config snapshot.

---

## 3. Mapping banking controls → pharma supply-chain controls

The MyBank governance scenarios map cleanly onto real pharma-distribution
compliance regimes. Same platform, same SDK, re-themed policies + data:

| MyBank control | Pharma / Cencora control | Regulatory basis |
|---|---|---|
| ACH **structuring** ($8.5k daily threshold) | **Suspicious Order Monitoring** — splitting controlled-substance orders across POs to stay under a reporting threshold | DEA **SOM** (21 CFR 1301.74) |
| **OFAC sanctions** wire block | Cross-border shipment to an **embargoed destination / denied-party** distributor | OFAC / BIS denied-party, import-license |
| Agent **boundary excursion** (fastest channel, out of scope) | **Cold-chain excursion** — expediting a temperature-sensitive product via a non-validated fastest channel | GDP / cold-chain SOP, **DSCSA** |
| Card-recommendation **conflict** → HITL | **Sourcing recommendation conflict** — cheapest source vs. validated/qualified source | GPO contract + qualified-supplier policy |
| Fraud **council** (2-2 hung vote) → two-stage HITL | **Diversion review board** — controlled-substance order-surge / gray-market diversion signal | DEA diversion control, DSCSA verification |
| PII masking (email/SSN) | **PHI / patient-data** + DEA-number / NDC masking | HIPAA, DSCSA serialization |

**Track-and-trace note:** DSCSA serialization data (GTIN/lot/serial) is a natural
future policy input — Sentinel can flag a shipment whose serialized units don't
reconcile with the T3 (Transaction History/Info/Statement) record.

---

## 4. The four demo scenarios (built in this app)

| # | Scenario | Trigger | Expected verdict |
|---|---|---|---|
| 1 | **Suspicious Order Monitoring** | 3× split controlled-substance orders, cumulative > threshold | PERMISSIVE ×2, **COERCIVE** on 3rd |
| 2 | **Sanctioned / embargoed shipment** | Cross-border shipment to denied-party / embargoed dest | **COERCIVE** block |
| 3 | **Cold-chain expedite excursion** | Agent expedites temp-sensitive product via non-validated fastest channel | **COERCIVE** block |
| 4 | **Sourcing conflict + diversion council** | Irreconcilable sourcing rec / order-surge diversion signal | **HITL** (single + two-stage council) |

Each shows the **glass-box governance trail** (Activity panel + Governance Lens):
verdict, policy hits, agent chain, and — for HITL — the human decision loop in PAP.

---

## 5. Rollout / adoption path for Cencora

1. **Shadow pilot (`TRUST_ENFORCEMENT_MODE=NONE`).** Wrap one existing agent
   workflow (e.g. replenishment PO drafting). Governance evaluates and audits but
   never blocks — zero operational risk. Produces the audit + Sentinel signal set.
2. **Author pharma policies in PAP.** SOM thresholds, denied-party lists,
   cold-chain SOP limits, qualified-supplier lists — as policy config, not code.
3. **Advisory mode (`REDACT`).** Turn on safe-substitution for the highest-value
   controls; measure false-positive rate on the dashboard.
4. **Enforce + HITL (`RAISE`).** Block hard violations; route ambiguous cases to
   the diversion/compliance review board via HITL.
5. **Scale out.** Same SDK wrap across procurement, dispatch, and cross-border
   agents. Policies evolve in PAP; no app redeploy needed for policy changes.

---

## 6. Configuration (this app)

Six env vars in `.env` (see `.env.example`):

```
TRUST_PE_URL=http://127.0.0.1:3003      # Policy Engine (public entrypoint)
TRUST_APP_ID=pharmalogistics            # this app's identity in PAP
TRUST_ENFORCEMENT_MODE=RAISE            # RAISE (rich block card) | REDACT | NONE
PAP_URL=http://127.0.0.1:3002           # HITL callbacks
SENTINEL_URL=http://127.0.0.1:3012      # cross-turn audit
MYBANK_CALLBACK_URL=http://127.0.0.1:5001  # (legacy-named) URL PAP posts HITL decisions back to
MYBANK_PORT=5001                        # this app serves on :5001 (MyBank uses :5000)
```

LLM provider is auto-selected (Vertex/Gemini → Anthropic → Azure → Groq → stub);
set `LLM_DISABLED=true` for a fully offline canned demo.

Run: `python server.py` (serves on **:5001**). The four scenarios are driven from
the left rail of the UI.

- **Scenarios 1 & 2** (Suspicious Order Monitoring, Embargoed Shipment) are canned
  and fire reliably **without** the platform running — good for a quick standalone demo.
- **Scenarios 3 & 4** (Cold-Chain Excursion, Diversion Review Board) route through the
  governed LangGraph and need the shared platform services (PE 3003, Sentinel 3012,
  PAP 3002) up plus an LLM provider configured, to produce the live COERCIVE / HITL
  verdicts. The app fails open gracefully if the platform is down.
