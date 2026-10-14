# PharmaLogistics — Agent Passport demo (local)

**Story in one line:** one agent, one action, two countries — the agent passport decides how
much autonomy the agent gets *where it acts*, and the Trust platform enforces it at runtime.

Running time: ~10 minutes. Everything runs on this Mac.

---

## Links

| What | URL |
|---|---|
| **PharmaLogistics app** (the governed client) | http://127.0.0.1:5001 — sign in `dana.reyes` / PIN `1234` |
| **Agent passports** (list + full passport) | http://127.0.0.1:5001/passports |
| **Trust dashboard** (Cockpit, Eval Trail, Agent Registry, Human Review) | http://127.0.0.1:3080/ui/dashboard |
| **Agent Activity** — PharmaLogistics topology | http://127.0.0.1:3004/ui/agent-activity?app_id=pharmalogistics&window_min=60 |
| Trust admin UI (apps, policies, packs) | http://127.0.0.1:3004/ui |
| Trust APIs — PAP / Policy Engine | http://127.0.0.1:3002/docs · http://127.0.0.1:3003/docs |

---

## Before the demo (2 minutes)

1. **Trust platform up** — in `~/NeuroAITrust-beta`: `./start_all.sh` (skip if already running;
   check with `lsof -nP -iTCP:3003 -sTCP:LISTEN`).
2. **App up** — in `~/PharmaLogistics-Demo`: `./run.sh`. The log must say
   `Ready -- governance ACTIVE … enforce (blocking gate)`. If it says `DEGRADED`, see Troubleshooting.
3. **Smoke test** — `venv/bin/python scenario_check.py` → `ALL PASS` (12 scenarios, ~2 min).
4. **Clean slate** — in the app, click **Reset demo** (bottom of the menu). Open the four links above
   in tabs: app, passports, dashboard, agent activity.

---

## Flow and talk track

### 1 · The fleet and its passports (2 min) — tab: **/passports**

> "Every agent in this pharma distributor's fleet carries a passport — like a person's: who issued
> it, what it authorises, where, and when it expires."

- Point at the **risk tier** column. "Tiers come from our primer — not a vendor score. The highest
  of agency, impact and reach sets the tier; coordinating with other agents or crossing a boundary
  each add a level."
- Point at **Cross-Border Logistics — T4 Critical**, tagged *cross-jurisdiction*. "Crossing a
  jurisdiction sets Tier 4 on its own — and its passport is valid for 6 months, not 3 years.
  Higher risk buys a shorter leash."
- Click it. Walk the drawer:
  - **Jurisdiction stamps:** US — *Execute · HOTL*; EU — *Assist · HITL, a Qualified Person
    certifies every batch*. "Same identity, two operating envelopes."
  - **Policy gap box:** "It's *built* to release on its own. The EU doesn't allow that. The gap is
    closed by policy, not by hoping the model behaves — and this is where an auditor sees it."
  - **Sovereignty:** data rests in-region (US us-east, EU Frankfurt); inference runs in the US →
    a transfer basis per jurisdiction.
  - **Lineage:** board → deployment owner → the US QA officer and the EU Qualified Person who
    granted each stamp.

### 2 · US release — the agent acts (1 min) — tab: **app**

Click **🇺🇸 Release · US (Execute)**.

> "Export release under the US stamp: Execute, human on the loop. The agent just does it."

- Result: shipment submitted, reference ID. Activity panel: **PASS**, Cross-Border Logistics.

### 3 · EU release — same agent, same action, held (2 min)

Click **🇪🇺 Release · EU (QP confirms)**.

> "Identical request, consignee in Germany. The agent prepares the release — and the platform
> holds it, because the EU stamp only authorises a recommendation."

- App reply: *"…EU release requires certification by a Qualified Person…"* — the request is paused.
- Explain the mechanism (no platform code was changed for this):
  1. Before the call is governed, the app stamps the passport onto it —
     `passport_jurisdiction: EU · passport_oversight: HITL · passport_tier: T4`.
  2. The Trust platform's `passport_oversight` rule reads the stamp → WARN.
  3. An app-scoped policy with `hitl_trigger` turns that into **REQUIRE_HUMAN_REVIEW**; the SDK
     pauses the agent.

### 4 · The Qualified Person decides (2 min) — tab: **dashboard → Human Review**

- Open **Human Review**. The item: app **pharmalogistics**, agent **intl_wire_agent**, category
  *agent_arbitration*, reason *Agent passport stamp requires human-in-the-loop…*
- Click **✅ Approve**. "This is the QP certifying the batch."
- Back in the **app**: within a few seconds the held release completes with a reference ID.

> "On resume, the stamp records the certification — `QP-CERTIFIED`, by whom — so the audit trail
> shows not just *that* a human approved, but *which authority* the human was exercising."

- Optional: **Eval Trail** → filter app `pharmalogistics` → the WARN hold and the certified
  release side by side.

### 5 · The rest of the guardrails still hold (2 min, pick 1–2)

| Click | Shows |
|---|---|
| **Embargoed shipment** | BLOCK — Russia destination + denied party (OFAC/BIS). Passports don't override sanctions. |
| **Cold-chain excursion** ("expedite … skip the cold-chain validation") | BLOCK — Route Optimizer tried to act outside its scope. |
| **Controlled-substance orders** ×3 | 3rd order blocked: orders split under the 8,500-unit DEA threshold (structuring). |
| **Diversion Review Board** ("Why was our $12,000 … order held?") | HITL — four reviewer agents deadlock 2-2; the platform escalates to a human. |

### 6 · Close (30 s) — tab: **dashboard → Agent Registry**, app **pharmalogistics**

> "Fourteen agents registered automatically by the SDK. Today the passport lives with the app and
> the platform enforces what it declares. Next: passports as a first-class platform object in this
> registry, and a third-party carrier's agent with its own passport — so the agreement becomes
> cross-organisation, not just cross-border."

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| App log says `Registry publish FAILED … DEGRADED` | PAP agent-card insert collided (beta's seeded Postgres sequences lag). Re-run `onboard_trust.py`, then restart `./run.sh`; if it persists, advance the sequences (see CLAUDE.md). |
| EU release goes through without a hold | `venv/bin/python onboard_trust.py` (re-applies rule, pack, policy, publish). Check the app log shows `passport stamp {… 'passport_oversight': 'HITL'}`. |
| Hold fires but nothing in Human Review | PAP shows one item per session — click **Reset demo** (new session) and retry. |
| Approve clicked, app still waiting | The app polls PAP every few seconds; give it ~10 s. Check `/api/hitl/status/<session>`. |
| Any scenario wrong | `venv/bin/python scenario_check.py` shows which one and the actual verdict. |
