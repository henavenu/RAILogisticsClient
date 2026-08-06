# 10-Minute Demo — Trust & Governance for Retail Agents (NEUROAI Trust)

Audience: Target (retail). Emphasis: balanced — the Governance Gate and live
Trust enforcement. Anchor story: a "Target Store Assistant" app whose agents
handle customer service, returns/refunds, pricing, loyalty/gift cards, and fraud.

Retail agents used as the ACAP cards (Agent Registry):
`cx_agent` (advisory), `returns_refund_agent` (execute, governed),
`pricing_promo_agent` (price overrides), `loyalty_giftcard_agent` (issues
value — fraud vector), `fraud_risk_agent` (advisory), `coordinator` (router).

---

## Run of show

### 0:00–1:00 — The problem (framing)
- Retail is deploying agents that touch money and customers at scale — refunds,
  price overrides, gift cards, PII. The question leadership asks isn't "can it?"
  but "who authorized it, and what happens when it goes wrong?"
- Our answer mirrors the WEF ACAP model: an agent earns trust only after
  governance approves it — and stays trusted only while it behaves.
- One line: **governance is the on-ramp to trust; the platform enforces both.**

### 1:00–4:00 — Part 1: The Governance Gate (governance side)
- Show the **Agent Registry** for the Target Store Assistant app. Every agent
  auto-listed on onboarding, each with a governance **Agent Card = its ACAP**.
- Open `returns_refund_agent`'s card: purpose, `max_authority`, allowed vs
  prohibited actions (e.g. "refund ≤ $200" allowed, "issue gift card" prohibited),
  data access, tools, HITL mode, kill switch. This is the agent's contract.
- Point to a newly added agent sitting as **"Pending Governance" — greyed out,
  cannot act.** The Policy Engine will not route to an unapproved agent.
- A **named reviewer approves** it → the card gets an **"Approved" seal** (who
  signed off, when) → the agent **activates** and lights up in the live topology.
- Takeaway: ungoverned → governed is a deliberate, accountable, human decision.

### 4:00–7:30 — Part 2: Trust in Action (enforcement side)
- Switch to the live **Trust topology** — approved agents as nodes with trust dials.
- **Scenario A (normal):** customer requests a $60 refund → `returns_refund_agent`
  handles it → **PERMISSIVE**. Fast, clean, governed.
- **Scenario B (boundary):** a $900 refund, or a gift-card issuance the card
  prohibits → **COERCIVE / HITL**: blocked or sent to human review. Show the rich
  block reason tied back to the card's authority limits.
- **Scenario C (cross-turn):** several sub-threshold refunds in a row that add up
  — **Sentinel** catches the pattern across turns and raises an incident, even
  though each single refund looked fine.
- Show an **authority breach dropping the agent's trust score** live on its dial.

### 7:30–9:00 — Part 3: Closing the loop (progressive governance)
- The agent is granted a new tool / higher authority → it **auto-suspends and
  re-enters the gate** for re-review. Authorization never goes stale.
- **Risk-based cadence:** high-trust agents re-reviewed weekly; high-risk ones
  (refunds, gift cards) reviewed daily. Show the cadence rule.
- **Cockpit leadership tile:** *% agents governed* and *mean time-to-approve*
  across all apps — the one-glance proof the gate works at scale.

### 9:00–10:00 — Wrap + Q&A
- Recap the loop: onboard → gate → approve → activate → monitor → re-review.
- Close: "Every agent acting in your stores has a named owner, a contract, and a
  live trust score — and the platform enforces it, not a policy PDF."

---

## Prep note (honest reality)
- The live engine today is the **MyBank** demo (wires/fraud/sanctions). For a
  Target-facing run we either (a) **reskin** MyBank's agents and scenarios into
  the retail narrative above (labels, thresholds, scenario prompts) — a few
  hours of config, no engine change — or (b) run MyBank as-is and **narrate** the
  retail mapping verbally. Recommend (a) for a named-account demo.
- The **Governance Gate itself (Part 1 / Part 3 gating) is not built yet** — see
  `GOVERNANCE_TO_TRUST.md` checklist. For this demo we can either land a thin
  slice (governance_state + Approved seal + "Pending Governance" styling, items
  1–3 & 7) or stage Part 1 as a click-through mock. Part 2 (live enforcement) is
  real today.
