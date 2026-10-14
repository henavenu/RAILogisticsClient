# Agent Passport demo — short script (~7 min)

**Open 3 tabs:** app http://127.0.0.1:5001 (dana.reyes / 1234) · passports http://127.0.0.1:5001/passports · dashboard http://127.0.0.1:3080/ui/dashboard
**Before:** click **Reset demo** in the app.

| # | Do | Say |
|---|---|---|
| 1 | **Passports tab** → point at the tier column → click **Cross-Border Logistics** | "Every agent has a passport. Tiers come from our primer. Crossing a jurisdiction sets Tier 4 on its own, valid 6 months. Same agent: US stamp *Execute · HOTL*, EU stamp *Assist · HITL — a Qualified Person certifies*." |
| 2 | **App** → **🇺🇸 Release · US** | "US stamp: the agent acts, a human monitors. Released." |
| 3 | **App** → **🇪🇺 Release · EU** | "Same agent, same action. The platform holds it: the EU stamp only allows a recommendation." |
| 4 | **Dashboard → Human Review** → **✅ Approve** | "The Qualified Person certifies the batch." |
| 5 | **App** (wait ~5 s) | "Released, and the audit trail records *QP-CERTIFIED* and who certified." |
| 6 | **App** → **Embargoed shipment** | "Passports don't override sanctions: blocked." |
| 7 | **App** → **Diversion Review Board** prompt | "Four reviewer agents deadlock 2–2; the platform escalates to a human." |
| 8 | **Dashboard → Agent Registry** → app *pharmalogistics* | "All agents registered by the SDK. Next: a third-party carrier's agent with its own passport — cross-organisation." |

**If something misbehaves:** EU release not held → `venv/bin/python onboard_trust.py`. Nothing in Human Review → Reset demo and retry (one item per session).

---

## Agentic AI governance features demonstrated

| Feature | Where in the demo |
|---|---|
| **Agent passport / governance record** — identity, owner, lineage of delegated authority, sovereignty, assurance, validity, revocation | Passports page |
| **Risk tiering from the primer** — agency, impact, reach; coordination and trust-boundary amplifiers; cross-jurisdiction sets T4 | Passports page (tier + basis) |
| **Human oversight models** — HITL, HOTL, HIC, stamped per jurisdiction | US release (HOTL) vs EU release (HITL) |
| **Cross-jurisdictional authority** — one identity, different operating envelopes per country | Steps 2–3 |
| **Runtime enforcement** — the passport stamp is evaluated on every governed action, before it executes | Step 3 hold |
| **Human-in-the-loop escalation and certification** — review queue, approve, resume, certification recorded | Steps 4–5 |
| **Auditability** — every hold and release in the Eval Trail with the authority exercised | Step 5 / Eval Trail |
| **Hard policy blocks** — sanctions / denied parties | Step 6 |
| **Multi-agent conflict detection** — deadlocked council escalated by the platform | Step 7 |
| **Agent discovery and registry** — SDK auto-registers the agent topology | Step 8 |
| **Policy as configuration** — passport enforcement added via Trust APIs, no platform code change | Explain at step 3 |
| *Also in the app (optional):* scope / boundary violation (cold-chain excursion), order-structuring detection (SOM), privacy masking of PII | Menu buttons |
