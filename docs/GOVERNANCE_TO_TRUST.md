# From Governance to NEUROAI Trust: An ACAP-Gated Agent Lifecycle

> Positioning and workflow reference mapping our Agent Registry onto the World
> Economic Forum's **AI Agent Capability and Authorization Profile (ACAP)**,
> introduced in *AI Agents in Action: A Playbook for Trusted Adoption,
> Authorization and Scaling* (Audrey Duet, WEF; Jason DePerro, Capgemini).

---

## 1. The alignment (narrative)

The Forum's ACAP is a *living authorization record* for an AI agent — part
employment contract, part operating manual, part performance review, part audit
trail — whose job is to specify the **minimum authorization an agent needs to
perform its role safely**. Our **Agent Registry is already an ACAP in all but
name.** Every agent carries a governance card (`config_agent_cards`) that
defines its permission boundaries up front — `purpose`, `capability`,
`max_authority`, `allowed_actions`, `prohibited_actions`, `data_access`, the
tools that route to it (`declared_tools`) and the agents it can hand off to
(`a2a_targets`) — alongside the human-oversight posture (`hitl_mode`,
`escalation`) and the degrade/suspend control (`kill_switch`). Wrapped around
that static profile is the *living* half of the record: a per-agent trust score,
evaluation and violation counts, and authority-breach tallies, all materialized
in real time from `audit_pap_log`. In other words, our card already satisfies
four of ACAP's five tenets — bounded permissions, reserved human oversight,
in-system enforceable controls, and an auditable living record.

The **fifth tenet — *progressive governance across the lifecycle* — is where the
Governance platform and the Trust platform connect.** ACAP requires that
governance reviews be *repeated* whenever an agent's authority changes (new
tools, new data access, new collaborators, new role). Today activation is
implicit: a card is seeded or synthesized and the agent simply appears in the
live topology. We propose making activation **explicit and gated**: an agent
card is authored and **reviewed on the Governance platform**, and **only upon
governance approval is the agent activated — surfaced and permitted to act — on
the Trust platform's "in-action" topology.** Any subsequent change to an
authority-relevant field automatically re-opens review and suspends the agent
until re-approved. This turns approval into an observable, enforceable control:
the Trust platform's live graph becomes a rendering of *what governance has
sanctioned*, and any suspension or authority breach detected in-action flows
back to re-open review — closing the loop between the two platforms.

---

## 2. Field mapping — Agent Card ⇄ ACAP

| ACAP element | Our field(s) | Source |
|---|---|---|
| Agent ID & identity | `app_id`, `agent_id`, `name`, `agent_type`, `framework`, `model_id`, `domain` | `config_agent_cards` |
| What it does / role | `purpose`, `capability` | `config_agent_cards` |
| Minimum authorization | `max_authority` | `config_agent_cards` |
| Which tasks it automates | `allowed_actions` | `config_agent_cards` |
| What it must **never** do | `prohibited_actions` | `config_agent_cards` |
| Systems / tools / data it accesses | `data_access`, `declared_tools`, `a2a_targets` | `config_agent_cards` + `config_app_agents` |
| Reserved human oversight | `hitl_mode` (`none`/`by_design`/`human`/`agent`/`agent_human`), `escalation` | `config_agent_cards` |
| Degrade / suspend after failure | `kill_switch`; lifecycle `status` (`active`/`observed`/`retired`/`shadow`) | `config_agent_cards` + `config_app_agents` |
| Living performance record | trust score (baseline 80, asymmetric), evaluations, violations, authority breaches | computed from `audit_pap_log` |
| Auditable authorization trail | `audit_pap_log` entries (CREATE/UPDATE, entity_type=`agent_card`) | `audit_pap_log` |
| **Progressive governance (NEW)** | proposed `governance_state` + re-review trigger | *to build* |

---

## 3. The governance-review → approval → activation lifecycle

A new `governance_state` on the agent card drives the gate. The agent is
**inert on the Trust platform** in every state except `approved`.

```
 draft ──submit──▶ under_review ──approve──▶ approved ──(authority change)──▶ under_review
   ▲                    │                        │                                  │
   │                    └──reject──▶ rejected     └──suspend / breach──▶ suspended ──┘
   └────────────────────── edit ──────────────────────────────────────────────────┘
```

- **`draft`** — card authored or auto-synthesized from topology; not visible as an active node.
- **`under_review`** — submitted to the governance queue; a reviewer must act.
- **`approved`** — the *only* state in which the agent activates on the Trust platform.
- **`rejected`** — stays out of the live topology; requires re-authoring.
- **`suspended`** — was live, then kill-switched / breached; re-enters review.

**Gate rule:** `agent is active on Trust platform  ⇔  governance_state == 'approved' AND kill_switch == 0`.

---

## 4. Steps on the **Governance platform** (PAP admin plane)

1. **Author / import the ACAP.** Operator fills the agent card (or the SDK's
   `/internal/register-agents` auto-synthesizes one from discovered topology).
   New card enters `governance_state = draft`. Undeclared/observed agents land
   as `shadow`/`observed` topology with a `draft` card — never auto-active.
2. **Submit for governance review.** Operator (or auto-submit on first
   registration) moves the card to `under_review` and enqueues a review item.
   Reuse the Human Review / alert queue with a new tier, e.g.
   `GOVERNANCE_REVIEW`.
3. **Review the authorization boundaries.** Reviewer inspects the ACAP against
   role: `purpose`/`capability`, `max_authority`, `allowed_actions` vs
   `prohibited_actions`, `data_access`, `declared_tools`/`a2a_targets`, and the
   oversight posture (`hitl_mode`, `escalation`, default `kill_switch`).
4. **Decide, with rationale.** Approve → `approved`; reject → `rejected`. The
   decision, reviewer, and rationale are written to `audit_pap_log`
   (entity_type=`agent_card`, action=`GOVERNANCE_DECISION`).
5. **Progressive re-review (ACAP tenet 4).** The `register-agents`
   reconciliation already detects deltas in `declared_tools` / `a2a_targets`;
   extend it (plus card PATCH) so that any change to an authority-relevant field
   — `max_authority`, `allowed_actions`, `prohibited_actions`, `data_access`,
   new tools, new `a2a_targets`, or a `hitl_mode` downgrade — automatically flips
   `approved → under_review` and **auto-suspends** the agent pending
   re-approval.

## 5. Steps on the **Trust platform** (live dashboard / in-action topo)

1. **Render only sanctioned agents as active.** Agent Registry and the Agent
   Activity topology show `approved` cards as live nodes with a trust dial;
   `draft`/`under_review`/`rejected` render as greyed **"Pending Governance"**
   nodes with no dial. (`pap/ui/dashboard.html` — `loadAgentRegistry`,
   `agentCardHTML`.)
2. **Enforce the gate in-system (ACAP tenet 3).** The Policy Engine consults
   `governance_state` before routing. An unapproved agent cannot act: PE returns
   COERCIVE (or a HITL hold) if a turn attempts to invoke it — approval literally
   gates execution, not just visibility.
3. **Run the living record.** Once `approved` + active, the ACAP updates live:
   trust score, evaluations, violations, authority breaches; Sentinel watches
   cross-turn; `kill_switch` and `hitl_mode` stay operable from the card modal.
4. **Reflect suspensions back to Governance.** A `kill_switch` toggle, a Sentinel
   authority breach, or an incident on the Trust platform sets
   `governance_state = suspended` and raises a `GOVERNANCE_REVIEW` alert — the
   agent drops out of the live topology and re-enters the Governance queue,
   closing the loop.

---

## 5a. Governance cadence (risk-scored re-review)

Approval is not one-and-done — the ACAP is a *living* record, so re-review runs
on a **cadence driven by the agent's live risk/trust score**, on top of the
event-triggered re-review of §4.5. The Trust platform already computes a
per-agent trust score (baseline 80, asymmetric weighting); invert it into a
risk band and schedule accordingly:

| Trust band | Risk | Re-review cadence | Rationale |
|---|---|---|---|
| High (≥ 75) | Low | **Weekly** | Stable, well-behaved agents; light touch. |
| Medium (50–74) | Elevated | **Every 2–3 days** | Drifting behavior; watch closely. |
| Low (< 50) | High | **Daily** (auto-suspend on breach) | Consequential or misbehaving; tight loop. |

Money-movement / high-`max_authority` agents (e.g. `intl_wire_agent`,
`domestic_agent`) default to the daily cadence regardless of score. Each
scheduled review re-enqueues the card as `GOVERNANCE_REVIEW`; a lapsed review
past its cadence window auto-suspends the agent until re-approved. Implement via
a scheduled reconciliation job that reads trust score and last-reviewed time.

---

## 5b. Governance assurances (audit & leadership)

Four controls make the gate accountable, not just functional:

1. **Versioned ACAP with diff view.** Every approved card is snapshotted as a
   version. On re-review, the queue shows *what authority changed* — added tool,
   widened `data_access`, raised `max_authority` — as a side-by-side diff against
   the last approved version. This is the primary auditor-facing artifact and the
   most legible re-review signal for a reviewer.
2. **Named sign-off / attestation.** Every decision records *who* approved and
   *why* (`reviewed_by`, `reviewed_at`, `review_notes`), written to
   `audit_pap_log`. "Approved" is an accountable human attestation, not a flag
   flip — the card modal shows the current approver and rationale.
3. **Leadership metric on the Cockpit.** A governance tile surfacing
   **% agents governed** (approved ÷ registered) and **mean time-to-approve**
   across all apps — one number that shows the gate is working at scale.
4. **Break-glass path.** An emergency *approve-with-conditions* that activates an
   agent immediately under a time-boxed, fully-audited exception (records the
   grantor, reason, and expiry; auto-reverts to `under_review` at expiry).
   Pre-empts the "what if review blocks a launch?" objection while keeping the
   exception on the record.

---

## 6. Implementation checklist (necessary build steps)

| # | Layer | Change | File(s) |
|---|---|---|---|
| 1 | Schema | Add `governance_state`, `reviewed_by`, `reviewed_at`, `review_notes` to `config_agent_cards`; new Alembic migration | `pap/core/models.py`, `pap/alembic/versions/` |
| 2 | Seed | Set demo cards to `approved`; add one `under_review` and one `draft` to showcase the gate | `pap/config/seed_agent_cards.py` |
| 3 | API | `POST /v1/agent-cards/{app}/{id}/submit`, `POST …/decide {approve|reject, notes}`; include `governance_state` in GET payloads; auto-`draft` on synth | `pap/api/routes.py` |
| 4 | Re-review trigger | On `register-agents` delta or PATCH to authority fields → flip to `under_review` + auto-suspend | `pap/api/routes.py` (`/internal/register-agents`) |
| 5 | Review queue | New `GOVERNANCE_REVIEW` alert tier + Human Review queue entries | `pap/core/models.py` (`GovernanceAlert`), dashboard Human Review tab |
| 6 | PE gate | Deny/hold routing to agents whose card is not `approved` | `policy-engine/main.py` |
| 7 | Trust UI | "Pending Governance" node styling; approve/reject controls in card modal; gate active dial on `approved` | `pap/ui/dashboard.html` (`agentCardHTML`, `loadAgentRegistry`) |
| 8 | Versioning | Snapshot approved card as a version; diff view of authority-field changes in the review queue | `pap/core/models.py` (new `config_agent_card_versions`), dashboard review UI |
| 9 | Attestation | Surface `reviewed_by` / `reviewed_at` / `review_notes` in the card modal; write decision to `audit_pap_log` | `pap/api/routes.py`, `pap/ui/dashboard.html` |
| 10 | Leadership tile | Cockpit tile: **% agents governed** + **mean time-to-approve** across apps | `pap/ui/dashboard.html` (Cockpit), `pap/api/routes.py` (metrics) |
| 11 | Break-glass | `POST …/break-glass {reason, expiry}` → `approved` with time-boxed exception + audit; auto-revert to `under_review` at expiry | `pap/api/routes.py`, scheduled reconciliation |

> Note: items 1 and 4 touch active-rework / coordination-sensitive areas
> (Alembic migrations are immutable — add a new one; the `register-agents`
> reconciliation is shared). Sequence 1→7; the gate (6) can ship behind a flag
> until seed + UI (2, 7) are ready.
