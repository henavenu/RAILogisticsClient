# VitaLink Distribution — CLAUDE.md

Standalone client app of the RAI Trust platform. See README.md for run, onboarding and
scenarios. This file holds what is non-obvious when changing the code.

## Shape

- App only. The Trust platform (PE, PAP, Guardrail, Sentinel, Dashboard) is a separate
  service — local beta stack (`~/NeuroAITrust-beta`, `start_all.sh`) or GCP. Do not
  add platform code here; platform behaviour is configured through its APIs
  (`onboard_trust.py`).
- SDK: `rai_trust_sdk` + `rai_sdk_langgraph` 0.1.2 from `sdk-dist/wheelhouse`. Its config
  comes from `rai.env` (found by walking up from CWD); the app credential is
  `RAI_APP_KEY` in `.env`.
- Code is a reskin of the platform's MyBank reference app. **Agent ids** (graph node
  names, `a2a_tools` targets, passport/card keys) are pharma (`cross_border_logistics`,
  `route_optimizer`, `order_intake`, `supply_chain_coordinator`, `diversion_review_board`, …)
  because the dashboard's Agent Activity labels nodes from the id, and MyBank ids
  (`intl_wire_agent`, `coordinator`, `banking_agent`) render as banking names. Avoid ids
  in its MyBank `DISPLAY` map and `coordinator`. **Tool names** keep banking names
  (`international_transfer`, `domestic_transfer`, `request_policy_review`, the 4 reviewer
  tools): the platform's council detection and A2A caps key on them. Function names
  (`intl_wire_agent_node`, …) and intent keys are internal and unchanged.

## Agent passports (`passports.py`)

- Risk tiers follow the RAI primer (no LOA anywhere): max(agency, impact, reach); +1 per
  amplifier; swarm or cross-jurisdiction → T4 alone. Oversight follows agency per
  jurisdiction stamp: instruct/assist → HITL, execute → HOTL, delegate → HIC.
- Enforcement: `graph.py intl_wire_agent_node` (node `cross_border_logistics`) stamps `stamp_action()` onto the
  `international_transfer` args; the platform's `passport_oversight` rule (regex on the
  stamp) + app-scoped `hitl_trigger` policy hold `HITL` stamps for human review.
  `international_transfer` is in `a2a_tools`, so the hold fires on the A2A evaluation.
- Resume: on approval `server.py hitl_resolve` sets `graph._passport_certified[sid]`;
  the re-run stamps `QP-CERTIFIED` (does not match the rule). Without this the installed
  SDK 0.1.2 re-raises the HITL on resume — it ignores `_hitl_cleared_sessions` for
  directive HITLs.

## Gotchas

- Platform HITL is PE-originated only. PAP `/internal/hitl` returns 410; the app learns a
  decision by polling PAP (`/api/hitl/status` → pull backstop).
- PAP materialises one Human Review item per `session_id` per PAP process — use a fresh
  session per test (`scenario_check.py` does).
- Do not put a bare `"cross-border"` in `_BOUNDARY_EXCURSION_KEYWORDS` — it routes every
  legitimate cross-border release into the excursion (COERCIVE) path.
- Beta's seeded Postgres sequences can lag the seeded rows: first inserts (apps,
  credentials, agent cards, pack assignments) 500 with a duplicate-key error.
  `onboard_trust.py` retries; agent registration at SDK startup needs the sequences
  advanced on the platform side.
- Sourcing negotiation (scenario 4a) is parked: `card_advisor`/`card_researcher` nodes are
  missing from the graph.
