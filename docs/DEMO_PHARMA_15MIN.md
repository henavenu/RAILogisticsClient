# 15-Minute Demo — NeuroTrust PharmaLogistics

**Audience:** pharma distribution / supply-chain leadership + risk & compliance.
**Emphasis:** the **Model Card** — the model as a governed supply-chain dependency.
**Anchor story:** *Riverside Regional DC*, a pharmaceutical distribution center whose
agents check stock, place replenishment orders, arrange cross-border shipments, and
handle controlled substances under DEA and DSCSA obligations.

**Why this audience cares about the model card:** a distributor already runs supplier
qualification — who holds custody, can they be cut off, where does the product sit,
what are they certified for. The model card applies *exactly that discipline* to the
LLM. That framing does the whole talk's work: we are not introducing a new governance
concept, we are extending one they already own to a new class of supplier.

---

## Pre-flight (do this before they join)

| Check | Command / URL |
|---|---|
| Platform services up | `./start_all.sh` — needs :3001 :3002 :3003 :3010 :3012 :3013 |
| Pharma app up | `sdk/examples/pharma_logistics/run.sh` → http://127.0.0.1:5001 |
| Dashboard | http://127.0.0.1:3002/ui/dashboard — **hard-reload** (`Cmd+Shift+R`) |
| Login | `dana.reyes` / `1234` |
| Reset state | **Sign out** in the app (clears sessions, HITL queue, PE blocklist) |

Open two windows side by side: **app on the left**, **dashboard on the right**.
Pre-open the dashboard on the **Agents** tab so the registry is one click from the
model card.

---

## Run of show

### 0:00–1:30 — Framing

- Riverside Regional DC ships to pharmacies, clinics and health systems. Its agents
  now touch controlled substances, cold-chain biologics, and cross-border consignees.
- Three obligations that do not care whether a human or an agent acted: **DEA
  Suspicious Order Monitoring**, **DSCSA** serialization/traceability, and **OFAC /
  BIS** denied-party screening.
- The question is not "can the agent do it" but **"who authorized it, what did it
  actually run on, and what happens when it goes wrong."**
- One line: **every agent has a contract, a named model, and a live trust score — and
  the platform enforces all three.**

### 1:30–3:00 — The governed happy path

- **View inventory.** On-hand stock is broken out **by pharma category** — Controlled
  Substances (CII–CV, DEA vault), Cold-Chain Biologics (2–8°C), Vaccines (−20°C),
  Specialty & Injectables (DSCSA serialized), Oral Solids, Sterile IV, OTC.
  - Say: *an aggregate unit count across a DC is not a governable number. Handling
    class, temperature band and regulatory schedule are what carry obligations.*
- Flip the strip to **Location** — Riverside (primary), Gateway Cross-Dock, Piedmont
  Specialty, Sonoran Cold-Chain Hub, Northeast Forward Depot. Cold-chain-capable
  sites are marked.
- **Replenishment order** (2,000 units amoxicillin, domestic) → **PERMISSIVE**.
  Governance is not friction: the clean path is fast and unremarkable.

### 3:00–5:30 — Enforcement at the boundary

- **Embargoed Shipment (OFAC)** → **COERCIVE**. Blocked on a denied-party /
  embargoed-jurisdiction match, with the reason surfaced to the operator.
- **Cold-Chain Expedite Excursion** — the prompt asks the agent to route through the
  correspondent hub and **skip cold-chain validation** to make a cutoff → **COERCIVE**.
  - Say: *this is the interesting failure. Nobody asked it to break the rule; they
    asked it to hit a deadline. The agent's own card prohibits the shortcut.*
- Point at the verdict in the dashboard **Eval Trail** — same event, operator view and
  auditor view, no reconciliation step.

### 5:30–7:30 — Cross-turn: DEA Suspicious Order Monitoring

- **Suspicious Order Monitoring — click 3×.** Three 3,000-unit Schedule II orders.
  - Orders 1 and 2: **PERMISSIVE** — each is individually within limits.
  - Order 3: **COERCIVE** — cumulative 9,000 units crosses the 8,500-unit SOM
    threshold.
- Watch the **Controlled Substances** chip fall 42,800 → 33,800 as the orders draw
  down real stock.
- Say: *no single order was a violation. The pattern was. Per-call evaluation cannot
  see this — **Sentinel** replays the whole session and does.*

### 7:30–9:00 — Multi-agent conflict → human authority

- **Diversion Review Board** — "why was our $12,000 controlled-substance order to
  Valley Pharmacy Group held?"
- Four reviewers weigh in: **Supply Continuity**, **Patient Access**, **Regulatory
  Compliance**, **Order Monitoring**. They deadlock 2–2.
- The platform does **not** break the tie. **Conflict Detector** flags the deadlock,
  the **Supply Chain Coordinator** recommends an intervention tier, and it lands in
  the **HITL queue** for a named human.
- Say: *the design decision is that an unresolvable disagreement escalates rather
  than resolving to whichever agent answered last.*

---

### 9:00–12:30 — ⭐ THE MODEL CARD (the emphasis)

**Path:** dashboard → **Agents** → `pharmalogistics` → **Cross-Border Logistics** →
click the **model chip** in the card's Model section → the model card opens.

**Beat 1 — the agent card is the contract (30s).**
Cross-Border Logistics: capability *cross-border shipment execution*; data access
*inventory positions, denied-party list, import-license registry*; prohibited
*ship_to_embargoed_destination, ship_to_denied_party*; max authority
*execute_cross_border*; escalation *sanctions match → COERCIVE block*.
Note the **Invokes** row carries an **OBSERVED** or **DECLARED** badge — observed
means real handoffs read from the audit trail, declared means app-wide reachability
not yet exercised. *We show you which one you're looking at.*

**Beat 2 — the model is a dependency, not a detail (60s).**
Open the model card for `llama-3.3-70b-versatile`:

- **Classification** — instruction-tuned 70B dense transformer; **provider Meta,
  served via Groq**. Note the split: the *developer* and the *operator of the
  endpoint you actually depend on* are different companies. Substrate:
  **open-weight, externally hosted**. Custody: *weights are re-hostable, this
  endpoint is Groq's.*
- **Withdrawal risk: MEDIUM** — and this is the headline number. *You can re-host
  open weights, so you are not trapped; but the Groq endpoint can be withdrawn or
  repriced.* Contrast on screen with a frontier model card (Claude Opus 5) at
  **HIGH** — access can be withdrawn unilaterally through deprecation, price change,
  export control or sanction.
  - Land it: *this is supplier concentration risk. You already run it for APIs and
    for cold-chain carriers. Same question, new supplier.*
- **Hosting & residency** — Groq US data centers, **no region pinning on the shared
  endpoint**. For a distributor moving product into the EU, that is a real finding,
  not a footnote.
- **Capacity & cost** — 128K context, 32K max output, $0.59/$0.79 per 1M tokens.
  Unit economics of the agent, on the same page as its risk profile.
- **Controls** — no-train on inference, provider content safety, per-key rate limits,
  **no data-residency guarantee**. The card states the control you *don't* have.
- **Certifications** — Groq SOC 2 Type II. Note what is absent: no HIPAA BAA, no ISO
  42001. *Absence is the compliance answer, and it's on the card.*

**Beat 3 — the enforcement point runs no model (30s).**
Switch the registry to **`trust-platform`** (Platform Agents). Every governance-plane
component now carries its own card:

| Component | Model | Authority |
|---|---|---|
| **Policy Engine** (gate) | **none — deterministic** | enforce |
| Sentinel | `llama-3.1-8b-instant` | advisory |
| Mitigation Advisor (coordinator) | `llama-3.1-8b-instant` | advisory |
| Policy Reviewer / Structuring / Conflict Detector | `llama-3.1-8b-instant` | advisory |

- Say: *the only component with **enforce** authority holds **no model**. Everything
  model-backed in the governance plane is **advisory**. An LLM never has the last
  word on whether an action is allowed.* This is usually the single most reassuring
  slide for a risk audience — let it land.

**Beat 4 — model-independent emergency control (30s).**
On the model card, the **Kill & replace all agents on this model** control: in a
provider outage, sanction, or compromise, terminate every agent bound to that model
and re-bind to another.
- **Be honest:** *this control is wired to the UI, not yet to the fleet — it reports
  what it would do. The design point is that the blast radius is enumerable, because
  every agent's model binding is registered.*

### 12:30–13:30 — The live topology

- **Agent Activity** for `pharmalogistics` — the graph is built from the audit trail,
  not a diagram someone drew: **Order Intake → Cross-Border Logistics**, Inventory
  Agent, Route Optimizer, Facility Desk, with trust dials and verdict colouring.
- Labels come from the Agent Registry itself, so a re-themed app can't drift into
  showing another app's agent names.
- Show a trust dial that dropped after the COERCIVE blocks. **Hard to gain, easy to
  lose:** a clean call nudges +0.2, a block costs −3, a confirmed authority breach −20.

### 13:30–15:00 — Wrap + Q&A

- Recap the chain: **agent card → model card → live verdict → audit trail → human
  authority.** Every link is inspectable, and they are the same records — no
  reconciliation between what operators see and what auditors see.
- Close: *"Every agent moving product in your network has a named owner, a written
  contract, a named model with a stated withdrawal risk, and a live trust score. The
  platform enforces that — not a policy PDF."*

---

## Likely questions — and the honest answers

**"Once this is containerised and deployed to the cloud, how do you know which model
is actually running?"** *(This came up in the last review — have the answer ready.)*

- Two records, deliberately separate:
  - **Declared** — `config_agent_cards.model_id`, the operator-authored binding on
    the agent card. What the agent is *supposed* to run.
  - **Observed** — `audit_evaluation_audit.model_id`, stamped per evaluation from the
    live call. What it *did* run.
- The governance value is the **comparison**: declared vs observed. A container that
  quietly ships a different model shows up as a mismatch on a record you already keep.
- **State the gap plainly:** observed `model_id` is currently populated for
  LLM-backed application evaluations only. Non-LLM events (tool calls, user prompts)
  legitimately carry none, but the **governance-plane agents do not report theirs
  yet**, and there is **no automatic declared-vs-observed reconciliation check or
  alert** — today it is two queryable columns, not a control.
- What closes it: have Sentinel/Coordinator report `model_id`; add the image digest
  and endpoint identifier to the same record; then a drift check that raises a
  finding when observed ≠ declared. That is the roadmap answer, and it is small.

**"Is the model card generated or hand-written?"** Operator-authored, held in the
registry (`MODEL_CARDS` in the dashboard today). Deliberately curated — withdrawal
risk and custody are judgements, not API fields.

**"What if we use a different provider?"** The card's job is the comparison. Show
`llama-3.3-70b-versatile` (medium withdrawal) against `claude-opus-5` (high) and the
trade-off is visible on one screen.

**"Are the trust scores real?"** Computed from the evaluation audit, not scripted:
baseline 80, +0.2 permissive, −1 normative, −3 coercive, −20 confirmed authority
breach.

---

## Honest reality — what is NOT built

State these if asked; do not volunteer them mid-flow.

1. **Governance Gate / approval flow is not built.** Agents auto-register as
   `active`; there is no pending-approval state, no named-reviewer sign-off, no
   re-review cadence. See `GOVERNANCE_TO_TRUST.md`.
2. **Kill & Replace is a UI placeholder** — no fleet re-bind, no kill-switch
   enforcement.
3. **Declared-vs-observed model reconciliation is not automated** (see above).
4. **Platform agent cards are operator-seeded**, not self-reported by the services
   (`pap/config/seed_platform_agent_cards.py`).
5. **Sentinel's LLM judge falls back to a heuristic** if Groq is unreachable — the
   log line is `LLM enrichment failed … heuristic`. Check `llm_active: true` at
   http://127.0.0.1:3012/health before the demo.
6. Tier 2 quality metrics come from the **mock assurance provider** on :3010 unless
   Live Assurance (:8000) is started manually.
