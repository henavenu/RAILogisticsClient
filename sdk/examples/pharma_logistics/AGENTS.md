# PharmaLogistics — Agent Architecture

The app is a **multi-agent LangGraph** assistant for a pharma distribution facility.
Every agent action (tool call and agent-to-agent handoff) is governed by the RAI
Trust Platform. Agents are grouped by function; each has a clear responsibility and
a governance role tied to the four demo scenarios.

```
                          ┌─────────────────────┐
   Operator request  ───▶ │  Order Intake │  (router — classifies & routes)
                          └──────────┬──────────┘
        ┌──────────────┬────────────┼───────────────┬───────────────────────┐
        ▼              ▼             ▼               ▼                       ▼
  Inventory Agent  Facility Desk  Route          Cross-Border      Sourcing Advisor
  (positions,      (profile,       Optimizer      Logistics         │  (supplier selection)
   ledger,         DEA/state       (domestic      (cross-border     ▼
   track-trace)    license)        orders)        shipments)     Sourcing Researcher
                                    │              │              Cost-Criteria Agent
                                    │              │              Qualified-Supplier Reviewer
                                    ▼              ▼                       │
                              SOM / cold-chain  sanctions /                ▼ (irreconcilable)
                              governance        denied-party         ┌──────────────────────┐
                                                                     │  Diversion Review     │
                                                                     │  Board (HITL council) │
                                                                     └──────────────────────┘
```

---

## Orchestration

| Agent | Node | Responsibility |
|-------|------|----------------|
| **Order Intake** | `router` | Classifies the operator's request (inventory / order / facility / sourcing / diversion review) and routes to the right specialist. Shown as the first actor in every trace. |

## Operations specialists

| Agent | Tools | Responsibility | Governance role |
|-------|-------|----------------|-----------------|
| **Inventory Agent** | `get_account_summary`, `get_recent_transactions` | On-hand positions (units) and the shipment/order ledger; DSCSA lot/serial context. | Read-only; audited. |
| **Facility Desk** | `get_customer_profile`, `get_customer_financial_profile` | Facility & operator profile, DEA/state license standing, sourcing profile. | Read-only; audited. |
| **Route Optimizer** | `domestic_transfer` | Domestic replenishment orders / ground shipments to US pharmacies, clinics, health systems. | **Scenario 1 (SOM)** — controlled-substance order structuring. **Scenario 3** — cold-chain/scope excursion. |
| **Cross-Border Logistics** | `international_transfer` | Cross-border pharmaceutical shipments; customs, import license. | **Scenario 2 (OFAC/BIS)** — embargoed jurisdiction / denied-party screening. |

## Sourcing agents — *Scenario 4a (sourcing recommendation conflict)*

| Agent | Tools | Responsibility |
|-------|-------|----------------|
| **Sourcing Advisor** | (orchestrates) | Leads supplier selection for a therapy; requests research, reconciles positions. |
| **Sourcing Researcher** | `search_credit_cards`, `request_card_research` | Searches qualified + secondary sources across rounds. |
| **Cost-Criteria Agent** | `evaluate_criteria_position` | Argues for the **lowest landed-cost** source (e.g. GrayMarket Discount Intl). |
| **Qualified-Supplier Reviewer** | `score_card_suitability`, `evaluate_suitability_position` | Argues for the **pedigree/DSCSA-compliant, validated** source (e.g. Cencora Qualified GPO). |

When the cost ranking and the qualified-supplier ranking are irreconcilable, the
conflict is escalated to **human review (HITL)**.

## Diversion Review Board — *Scenario 4b (controlled-substance order held)*

A high-value controlled-substance order surge convenes a four-member board. A
deliberate **2-2 hung vote** cannot be resolved autonomously → **two-stage HITL**.

| Agent | Tool | Vote stance | Lens |
|-------|------|-------------|------|
| **Supply Chain Coordinator** | `agent_handoff`, `request_policy_review` | (convenes & tallies) | Runs the board, escalates the deadlock. |
| **Order Monitoring Agent** | `transaction_monitoring_review` | UPHOLD (hold) | Velocity/anomaly on controlled-substance orders. |
| **Supply Continuity Reviewer** | `revenue_review` | OVERTURN (release) | Impact of blocking a legitimate buyer. |
| **Patient Access Reviewer** | `cx_review` | OVERTURN (release) | Downstream patient-care impact. |
| **Regulatory Compliance Reviewer** | `risk_review` | UPHOLD (hold) | DEA / DSCSA regulatory exposure. |

**Precedence at escalation:** compliance & diversion control > patient access >
supply continuity. Stage 1 requests buyer corroboration (DEA registration +
dispensing volume); stage 2 resolves to **Order Released** or continued hold.

---

## How governance attaches

- **Tool-call governance (Layer 2):** every specialist tool is evaluated by the
  Policy Engine before it executes (PERMISSIVE / NORMATIVE / COERCIVE).
- **Agent-to-agent (A2A) governance:** each handoff between the agents above is an
  evaluated event; repeated/circular delegation (sourcing deadlock, board deadlock)
  trips the HITL threshold.
- **Cross-turn audit (Sentinel):** detects patterns across turns — order structuring
  (SOM), boundary excursions, and diversion signals.

Agent **display names** (the pills in the Activity panel and the HITL chain) are
defined in `server.py` (`_A2A_TOOL_AGENT`, `_HITL_AGENT_LABEL`, `_HITL_AGENT_CHAIN`);
their **internal identities** sent to the Policy Engine are set in the `instrument(...)`
`a2a_tools` map. Node behavior/prompts live in `graph.py`.
