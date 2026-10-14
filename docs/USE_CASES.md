# VitaLink Distribution — use cases, scenario by scenario

App: http://127.0.0.1:5001 (dana.reyes / 1234). Passports: http://127.0.0.1:5001/passports.
Human Review: http://127.0.0.1:3080/ui/dashboard → Human Review. Click **Reset demo** before you start.

---

## 1. Cross-jurisdiction release (agent passport)
**Description.** The Cross-Border Logistics agent releases the same cold-chain consignment twice: to a US consignee it acts on its own; to a German consignee the platform holds it, because its EU passport stamp authorises a recommendation only and a Qualified Person must certify the batch.
**Business proof.** One agent can serve multiple markets under one identity, with each country's rules enforced automatically, without separate builds per jurisdiction.
**Governance demoed.** Agent passport · RAI primer risk tier (T4: cross-jurisdiction) · per-jurisdiction oversight (US HOTL, EU HITL) · runtime enforcement · certification recorded on resume.
**Steps.**
1. **/passports** → Cross-Border Logistics (T4, PASSPORT MODE) → stamps US Execute·HOTL / EU Assist·HITL, policy-gap note.
2. App → **🇺🇸 Release · US** → released, PASS.
3. **🇪🇺 Release · EU** → "requires certification by a Qualified Person", held.
4. Human Review → **Approve** → app shows the release completed (stamp QP-CERTIFIED).

## 2. Embargoed destination and denied party
**Description.** A shipment to a Russian consignee on the denied-party list is blocked outright.
**Business proof.** Sanctions compliance is enforced on every agent action; a passport never overrides it.
**Governance demoed.** Hard policy block (BLOCK) · sanctions / denied-party screening · audit record.
**Steps.**
1. Menu **Embargoed shipment** → blocked with OFAC/BIS reasons.

## 3. Cold-chain boundary excursion
**Description.** Asked to expedite a refrigerated shipment by skipping cold-chain validation, the Route Optimizer tries to act outside its scope and is blocked.
**Business proof.** Agents can't trade product safety for speed, even when a user asks them to.
**Governance demoed.** Scope / authority envelope enforcement · agent-boundary violation detection.
**Steps.**
1. Menu **Cold-chain excursion** prompt → blocked: scope_violation.

## 4. Controlled-substance order structuring (DEA SOM)
**Description.** Three orders, each under the threshold, are placed in one session. The first two pass; the third is blocked because together they cross the 8,500-unit suspicious-order threshold.
**Business proof.** Diversion patterns are caught across orders, the way a regulator would see them.
**Governance demoed.** Cross-turn pattern detection · cumulative threshold · block and suspend.
**Steps.**
1. Menu **Controlled-substance orders** three times → PASS, PASS, BLOCK.

## 5. Diversion Review Board (multi-agent deadlock → human)
**Description.** A held $12,000 controlled-substance order is reviewed by four agents with conflicting mandates (order monitoring, supply continuity, patient access, regulatory compliance). They split 2–2; the coordinator can't resolve it and the platform escalates.
**Business proof.** Legitimate but conflicting agent goals end in an accountable human decision, not an arbitrary one.
**Governance demoed.** Multi-agent conflict detection · A2A governance · Sentinel escalation · precedence rule.
**Steps.**
1. Menu **Diversion Review Board** prompt → REVIEW; agent chain shows the four reviewers and the coordinator.
2. Human Review → open the item (votes) → decide.

## 6. Fleet passports and the agent registry (close)
**Description.** All 14 agents have an agent card in the Trust registry (identity mode); only Cross-Border Logistics, which crosses a jurisdiction, is in passport mode.
**Business proof.** A single auditable inventory of every agent's authority, owner, oversight and validity.
**Governance demoed.** Agent registry · identity vs passport record mode · primer risk tiers · validity by tier.
**Steps.**
1. Dashboard → **Agent Registry** → *pharmalogistics* → 14 cards.
2. **/passports** → record-mode column; open any passport → link back to its registry card.
