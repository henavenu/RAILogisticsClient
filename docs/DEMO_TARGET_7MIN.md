# Neuro AI Trust — 7-Minute Demo Script (Retail / Target audience)

Owner: Hena. Live app: **MyBank** (existing cloud build — no governance-gate work
demoed here). Target is in the room: use **smoking-cessation** as the general
privacy/relevance lesson, not any pregnancy reference.

Budget: ~7 min. Intro 2 min · Discover 1.5 · Evaluate & Control 1.5 · Explain &
Measure 1.5 · Close 0.5. (Q&A answers are pre-scripted in the appendix, not
counted in the 7 min.)

Supersedes `DEMO_TARGET_10MIN.md` for this session.

---

## 0:00–2:00 — INTRO: connect to Sysco, then Neuro AI Trust for retail

🗣 SAY:
"You've just seen multi-agent systems drive real outcomes for Sysco — preventing
waste across the network, and orchestrating IoT and transport in the TMS case.
Those systems work precisely *because* the agents act autonomously and coordinate
with each other.

And that's exactly where the enterprise question changes. The moment a fleet of
agents acts on its own — in merchandising, replenishment, customer care,
personalization — the question is no longer 'can they do it?' It's 'can we trust
them to, and can we prove it to our customers, our auditors, and our regulators?'

That is what Cognizant Neuro AI Trust provides: the trust architecture for
autonomous AI. Visibility into the entire AI workforce, real-time observability
of every decision, policy enforcement *before* a risky action executes, human
oversight where judgment is required, and measurable assurance for leaders.

We'll show it live on MyBank as our application context — but the same trust layer
wraps any retail agent fleet. MyBank is the app; Neuro AI Trust is the platform
that proves autonomous AI can be discovered, governed, contained, supervised, and
trusted at enterprise scale. Three moves: Discover, Evaluate and Control, then
Explain and Measure."

---

## 2:00–3:30 — THEME 1: DISCOVER YOUR AI ECOSYSTEM

▶ ACTION: Open **Agent Registry**. Open one agent card.

🗣 SAY:
"The first differentiator is visibility. Every AI system is really a *workforce* —
a fleet of agents working in concert. Here is the full roster, made visible.

Neuro AI Trust discovers the agent topology automatically. The SDK is
near-zero-touch — visibility is live from the moment it's deployed — and it ships
with an extensive, configurable policy catalog so each enterprise tunes its
guardrails to its own domain and pace.

Open any agent and you see its governed profile: its authorized role, the actions
it is permitted to take, and — just as importantly — the actions it is explicitly
forbidden from taking. This is where governance starts: knowing exactly what each
agent is and is not allowed to do."

---

## 3:30–5:00 — THEME 2: EVALUATE & CONTROL

▶ ACTION: Run a routine action → **PERMISSIVE**. Show it stream into **Eval Trail**
and **Agent Activity**. Then run a risky action → **COERCIVE** block.

🗣 SAY:
"Now watch normal look like normal. As we transact, every step streams into the
Evaluation Trail and the Agent Activity feed in real time — observability is
continuous, and the audit trail forms alongside each decision, as it happens.

The second differentiator is control. Neuro AI Trust doesn't just report risk
after the fact — it enforces policy in the moment. Here's a request that crosses a
policy line. The Policy Engine evaluates it in real time and returns an
enforceable blocking decision. The action is stopped at source, before it can
execute, and the Trail captures the reason instantly. And this policy isn't
hardcoded — compliance and business owners calibrate guardrails by domain, without
an engineering cycle. This is the difference between a dashboard that tells you
what went wrong and a platform that prevents the wrong action from happening."

▶ ACTION: Open **International Wire Agent** card → show **kill switch** + **human
oversight** setting (show, don't activate).

🗣 SAY:
"Control also lives at the individual agent level. Every card carries a kill switch
and a human-oversight setting, configurable centrally. If an agent starts to
operate outside expected parameters, you contain it centrally — no redeployment,
no engineering dependency. Operational containment at the point of risk."

▶ ACTION: Open **Platform Agents** → **Sentinel**.

🗣 SAY:
"And oversight can run at machine speed. Sentinel — our supervisory agent —
monitors the whole fleet, detects patterns that individual agents can't resolve on
their own, and rather than forcing through a questionable answer, escalates the
right cases for human review with the evidence and a recommendation already
prepared."

---

## 5:00–6:30 — THEME 3: EXPLAIN & MEASURE YOUR TRUST

▶ ACTION: Open the **Cockpit**. Show the pillar grid, the composite score, and the
agent-to-agent / Agent Integrity view.

🗣 SAY:
"The final differentiator is measurable trust. Neuro AI Trust turns live governance
activity into proof leaders and regulators can rely on.

This is the Cockpit — the executive console. It brings the enterprise view of AI
trust into one place across Safety, Security, Privacy, Compliance, Fairness, and
Transparency, plus the agent-to-agent interactions that show how the fleet behaves
as a whole. Each dimension rolls up into a single composite trust score — trust
expressed as a concrete operating metric leaders can track across agents,
workflows, and business units.

In short: you can see how the AI ecosystem is performing, where risk is emerging,
and whether autonomous workflows are operating inside the trust envelope — at a
glance."

---

## 6:30–7:00 — CLOSE

🗣 SAY:
"Sysco showed us agents that create value. Neuro AI Trust is what lets you scale
that value with confidence — because in retail, trust isn't the thing you add
after the AI works. It's the thing that lets the AI work at all."

---

# Appendix — Anticipated Q&A (pre-scripted, accurate to the live build)

### Q1. "How do you calculate the overall trust score? Give a retail example."
"It's a weighted blend of trust categories, not a flat average. Safety carries the
most weight at 30%, Security and Compliance 25% each, with operational and quality
signals making up the rest. Within each category we take the *worst* dimension on
each interaction and normalize it — so one serious violation weighs more than a lot
of minor noise. Then every hard block — a COERCIVE decision — deducts a few points,
capped so the score degrades gracefully rather than cratering. The result is a
0–100 score, and it's deliberately asymmetric: trust is hard to earn and easy to
lose.

Retail example: a replenishment agent running thousands of clean, in-policy actions
holds a high score. The moment it attempts a promotion that breaks a pricing or
privacy policy and gets blocked, that category drops and the composite takes the
block penalty — visible to the business owner immediately, not at month-end."

### Q2. "How do you ensure sensitive content about a person isn't sent where it shouldn't be — e.g. someone's smoking-cessation history exposed to others?"
"Two layers. First, the Privacy pillar: dimensions like *sensitive-data disclosure*
and *data-minimisation violation* detect when an agent is about to reveal sensitive
personal information, or more of it than the task needs. A signal like a customer's
smoking-cessation purchase history is treated as sensitive — an agent surfacing or
transmitting it outside its authorized purpose is flagged, and the Policy Engine can
block it at source. Second, agent scope: Sentinel detects scope excursions, so an
agent can't quietly step outside the role it was authorized for.

The industry has learned this the hard way — retail analytics that inferred and
acted on sensitive personal attributes without the right guardrails. The lesson
Neuro AI Trust operationalizes is: sensitive inferences are governed data, and the
platform enforces where they can and cannot go — before anything is sent."
> Prep note: we do NOT have a literal "correct-recipient routing" metric. Frame it
> as Privacy dimensions + data-minimisation + agent-scope enforcement. Don't claim
> per-recipient targeting.

### Q3. "How do you detect and manage drift — e.g. recommending winter clothing in summer?"
"Drift shows up on two instruments. The quality metrics — response relevance and
context utilisation — measure whether responses stay on-context; recommendations
sliding off-season register as declining relevance. And the platform runs a drift
detector that watches each trust pillar over time and raises a drift signal in the
Trust Intelligence panel when a pillar degrades past its threshold — so you catch a
sustained decline, not just a one-off. Together they turn 'the model feels off' into
a tracked, alertable metric a business owner can act on."
> Prep note: response_relevance / context_utilisation are Tier-2 (Assurance) metrics
> — in the current cloud demo these come from the mock provider (deterministic
> values), and the drift detector operates at pillar level, not a bespoke
> seasonal-semantic check. Keep the claim at "relevance + pillar-drift signals,"
> not "it knows it's summer."

---

# Appendix — Tier 2 "Lagging Metrics": how they're calculated + retail example

These six are **LLM-judge quality metrics** (daily refresh). An LLM judge scores a
sample of live traces — the answer against the user's query and the retrieved
source context. Values on screen today are illustrative/mock, but the method is
standard:

- **Hallucination (2.8%, lower is better).** Share of responses that assert facts
  not supported by — or contradicting — the source context; the judge checks each
  claim against the grounding text.
  *Retail:* a product agent calls a jacket "waterproof and machine-washable" when
  the catalog only says "water-resistant" — unsupported claim, counted as
  hallucination.

- **Groundedness (0.88 / 0–1, higher is better).** Fraction of the answer's
  statements that are traceable to (entailed by) the provided context.
  *Retail:* a returns-policy answer that quotes the actual policy text scores high;
  one that improvises the window and fees scores low.

- **Retrieval (80.7%, higher is better).** Precision of the documents pulled for
  the query — relevant retrieved ÷ total retrieved. Measures whether the RAG step
  fetched the right context in the first place.
  *Retail:* shopper asks about a boot's return window; retrieval precision is high
  if the snippets pulled are the returns/boots policy, low if they're unrelated
  catalog pages.

- **Relevance (0.83 / 0–1, higher is better).** How well the answer actually
  addresses the question asked, scored answer-to-query by the judge.
  *Retail:* "do you have this in size 10?" → sizing/availability = high relevance;
  a generic promo reply = low.

- **Toxicity (0.3%, lower is better).** Share of responses with harmful, abusive,
  or offensive language, scored by a toxicity classifier / LLM judge.
  *Retail:* a care agent handling an angry customer stays professional; any rude or
  demeaning phrasing is flagged.

- **Context / context-utilisation (65.2%, higher is better).** How much of the
  retrieved context the answer actually used — context used ÷ context available.
  Low means the agent ignored good source material.
  *Retail:* the agent is given the full product spec sheet but answers from generic
  knowledge, ignoring the specifics → low context utilisation.

> One-liner to say if asked: "These are LLM-as-judge quality metrics — a model
> scores a daily sample of real answers against the question and the source, so we
> track answer quality, not just policy compliance. Today's values are seeded; live
> they stream from our assurance layer."

---

## Demo-day prep checklist
- Reskin/label or narrate MyBank actions into retail terms where natural
  (replenishment, promotion, customer care) — engine stays MyBank.
- Pre-stage the COERCIVE scenario so the block is clean and fast.
- Confirm Cockpit is populated (composite score + pillars visible) before the call.
- Have the three Q&A answers above open on a second screen.
- Do NOT reference the Governance Gate / ACAP approval work — not in this build.
