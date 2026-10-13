# Neuro Trust for Retail — 2-Slide Outline (Owner: Hena)

Placement: after the Vector case studies (Vector 2 — Sysco multi-agent waste
prevention; Vector 3 — Sysco IoT TMS) and the Retail Foundry slide. The audience
has just seen agents driving business outcomes; these two slides answer the next
question every leader asks: *how do we deploy a fleet of these safely, at scale?*

Cross-cutting note: trust & governance spans all three tracks (Warehouse
Modernization, Merchandising, Data Science) — position it as the horizontal that
makes each vertical's agents deployable.

---

## SLIDE 1 — "Neuro Trust for Retail: Governing the Agent Fleet"

Purpose: connect the Sysco/vector demos to the need for governance, and introduce
the platform in one frame.

Headline: **From agents that work to agents you can trust.**

Talking points (3–4 bullets on slide):
- The vectors just showed multi-agent systems creating outcomes. Scaling them
  means every agent needs three things: a **contract**, a **gate**, and **live
  oversight**.
- **Agent Card = the contract (ACAP).** Aligned to the World Economic Forum's
  new *AI Agent Capability & Authorization Profile*, each agent has an auditable
  record: its purpose, authority, what it may and must never do, what data and
  tools it touches, and its human-oversight mode.
- **The Governance Gate = ungoverned → governed.** Agents are listed on
  onboarding but cannot act until a named reviewer approves the card. Approval
  carries an "Approved" seal — activation is an accountable human decision.
- **Trust in action = live oversight.** Approved agents run in a live topology
  with a trust score; the Policy Engine enforces limits in real time and Sentinel
  watches behavior across turns.

The one line to land: **Governance is the on-ramp to trust — and the platform
enforces both.**

Suggested visual: the lifecycle loop —
`Onboard → list in registry → Governance Gate → Approved seal → Activate → Live trust monitoring → Re-review`
with an agent card thumbnail and the WEF/ACAP tie-in badge.

---

## SLIDE 2 — "Neuro Trust in Action: Retail Scenarios"

Purpose: make it concrete with retail moments the audience recognizes; prove the
platform catches what matters.

Headline: **The controls that keep an agent fleet inside the lines.**

Three retail moments (one row each on slide):
1. **Money & value actions.** A refund or gift-card issuance beyond the agent's
   authorized limit → **blocked or sent to human review** (COERCIVE / HITL), with
   the reason tied back to the agent's card.
2. **Privacy & protected categories.** An agent that infers a sensitive
   attribute (e.g., health/pregnancy from purchase history) and tries to act on
   it in an outbound message → **blocked**; the card's prohibited-actions and a
   human review stop the disclosure before it leaves. (The classic retail
   cautionary tale, prevented by design.)
3. **Multi-agent oversight (ties back to Sysco).** As agents hand off to each
   other, Sentinel detects cross-turn patterns no single step would flag — the
   same fleet-level oversight the Vector demos need to run safely.

Leadership proof point (footer strip):
- **% agents governed** and **mean time-to-approve** on the Cockpit — one glance
  shows the gate working across every app and track.
- Governance re-review runs on a **risk-based cadence** (weekly for low-risk,
  daily for high-risk agents) so authorization never goes stale.

Close line: **"Every agent in your stores has a named owner, a contract, and a
live trust score — enforced by the platform, not a policy PDF."**

Suggested visual: split panel — live topology on the left (green governed nodes,
one greyed "Pending Governance"), the three scenario cards on the right, Cockpit
metric strip along the bottom. Embed demo/video link (mirrors Shanker's Vector
slides).

---

## Prep / honesty notes for Hena
- **Live today:** Part-2 live enforcement (PERMISSIVE/COERCIVE/HITL, Sentinel
  cross-turn) on the MyBank engine.
- **Not built yet:** the Governance Gate (gating on approval) and the
  privacy/protected-category scenario — see `GOVERNANCE_TO_TRUST.md` and the
  privacy-proxy assessment. For the call, either land a thin slice or present
  those beats as designed mockups / video.
- **Retail framing:** the demo engine is banking; reskin agent labels/thresholds
  into retail (returns, pricing, loyalty) or narrate the mapping.
- **Sensitivity flag:** the pregnancy-inference example is a well-known Target
  incident. Powerful as an industry lesson; if Target is in the room, frame it as
  "learning the industry has internalized," not a callout.
