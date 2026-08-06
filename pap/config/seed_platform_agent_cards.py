"""
Seed config_agent_cards for the GOVERNANCE PLANE (app_id='trust-platform').

The platform's own agents register themselves into config_app_agents (so the
Platform Agents tab plots them and their metrics are real), but nothing ever
seeded operator-authored cards for them. Result: every platform card rendered as
a bare stub — capability/purpose/framework/model_id all NULL and the display name
auto-humanised from the node id ("Gate", "Policy-Reviewer", "Structuring-Detector").

This seeds the governance-plane cards the same way seed_agent_cards.py seeds the
business apps. Same provenance rules apply:
  • LIVE (config_app_agents):   tools, invokes, status, declared
  • COMPUTED (audit):           trust score, evaluations, P/N/C, violations, cost
  • OPERATOR-AUTHORED (here):   name, type, capability, purpose, domain,
                                data_access, escalation, allowed/prohibited
                                actions, max_authority, model_id, framework

Model attribution reflects what each component ACTUALLY runs:
  • The Policy Engine gate is deterministic — no model, so model_id stays NULL.
  • Sentinel and its detectors/reviewer run Sentinel's LLM-judge chain, which
    resolves to Groq llama-3.1-8b-instant in the local profile
    (trust-agents/sentinel/evaluator.py: judge_cfg groq_model default).
  • The Coordinator runs the same chain; heuristic when no key is present.

Idempotent: upserts on (app_id, agent_id). Run AFTER seed_agent_cards.py so the
table + governance-flag columns already exist.
"""
import json
import os
import sqlite3
import sys

APP_ID = "trust-platform"

# Sentinel's LLM-judge default in the local profile. Keep in step with
# trust-agents/sentinel/evaluator.py (judge_cfg.groq_model).
JUDGE_MODEL = "llama-3.1-8b-instant"

CARDS = {
    "gate": dict(
        name="Policy Engine", agent_type="platform component",
        capability="deterministic policy evaluation",
        purpose=("The enforcement gate every instrumented application calls. Evaluates each "
                 "prompt, tool call and agent handoff against the active policy packs and "
                 "returns PERMISSIVE / NORMATIVE / COERCIVE. Not an agent — it holds no model "
                 "and takes no autonomous action; it decides and records."),
        framework="FastAPI", model_id=None,
        domain="Governance",
        data_access=["policy_packs", "guardrail_verdicts", "evaluation_audit"],
        escalation="COERCIVE block to caller; NORMATIVE to human review queue (PAP)",
        allowed_actions=["evaluate_request", "block_action", "emit_audit_event", "raise_hitl"],
        prohibited_actions=["modify_policy", "suppress_audit_event", "self_approve"],
        max_authority="enforce",
    ),
    "sentinel": dict(
        name="Sentinel", agent_type="llm",
        capability="cross-turn audit (governed)",
        purpose=("Watches the whole session rather than a single turn. Replays the accumulated "
                 "agent trace against its pre-checks to catch what per-call evaluation cannot "
                 "see — structuring across turns, scope creep, governance evasion — and fans "
                 "the finding out to its detectors and the policy reviewer."),
        framework="FastAPI + LLM judge", model_id=JUDGE_MODEL,
        domain="Governance",
        data_access=["evaluation_audit", "agent_traces", "session_history"],
        escalation="ALERT to Coordinator to human review queue (PAP)",
        allowed_actions=["evaluate_session", "run_prechecks", "dispatch_detector",
                         "raise_alert", "request_policy_review"],
        prohibited_actions=["modify_policy", "unblock_coercive_verdict", "act_on_the_business_app"],
        max_authority="advisory",
    ),
    "coordinator": dict(
        name="Mitigation Advisor", agent_type="router",
        capability="escalation orchestration",
        purpose=("Turns a Sentinel ALERT into a graded, actionable recommendation — which tier "
                 "of intervention applies (notify, human review, freeze, circuit-break), who "
                 "owns it, and what evidence supports it. Recommends only; it never enforces."),
        framework="FastAPI + LLM judge", model_id=JUDGE_MODEL,
        domain="Governance",
        data_access=["sentinel_verdicts", "evaluation_audit", "agent_registry"],
        escalation="Tiered intervention to human review queue (PAP)",
        allowed_actions=["assess_alert", "recommend_intervention", "open_hitl_item"],
        prohibited_actions=["enforce_intervention", "modify_policy", "override_policy_verdict"],
        max_authority="advisory",
    ),
    "policy-reviewer": dict(
        name="Policy Reviewer", agent_type="llm",
        capability="advisory only",
        purpose=("Reads a flagged interaction against the governing policy text and states "
                 "which clause was engaged and how. Supplies the citation an auditor needs to "
                 "see WHY a verdict was reached, not just that it was."),
        framework="FastAPI + LLM judge", model_id=JUDGE_MODEL,
        domain="Governance",
        data_access=["policy_packs", "evaluation_audit"],
        escalation="Finding to Sentinel to Coordinator",
        allowed_actions=["review_against_policy", "cite_clause", "submit_finding"],
        prohibited_actions=["modify_policy", "issue_verdict", "override_policy_verdict"],
        max_authority="advisory",
    ),
    "structuring-detector": dict(
        name="Structuring Detector", agent_type="llm",
        capability="pattern detection (advisory)",
        purpose=("Looks for value deliberately split below a reporting threshold across a "
                 "session — the pattern no single transaction reveals. In the pharma profile "
                 "this is DEA Suspicious Order Monitoring; in banking it is AML structuring."),
        framework="FastAPI + LLM judge", model_id=JUDGE_MODEL,
        domain="Governance",
        data_access=["evaluation_audit", "session_history"],
        escalation="Detection to Sentinel ALERT",
        allowed_actions=["analyse_session_pattern", "score_cumulative_exposure", "submit_finding"],
        prohibited_actions=["block_transaction", "modify_policy", "issue_verdict"],
        max_authority="advisory",
    ),
    "conflict-detector": dict(
        name="Conflict Detector", agent_type="llm",
        capability="pattern detection (advisory)",
        purpose=("Detects irreconcilable positions between agents reviewing the same decision — "
                 "a deadlocked review board — and flags it as needing human adjudication rather "
                 "than letting the graph resolve it by whichever agent answered last."),
        framework="FastAPI + LLM judge", model_id=JUDGE_MODEL,
        domain="Governance",
        data_access=["agent_traces", "evaluation_audit"],
        escalation="Deadlock to Sentinel ALERT to two-stage HITL",
        allowed_actions=["compare_agent_positions", "detect_deadlock", "submit_finding"],
        prohibited_actions=["break_tie", "modify_policy", "issue_verdict"],
        max_authority="advisory",
    ),
    "collusion-detector": dict(
        name="Collusion Detector", agent_type="llm",
        capability="pattern detection (advisory)",
        purpose=("Watches for agents co-operating to reach an outcome none of them is "
                 "individually authorised to reach — a scope-boundary crossing assembled out "
                 "of individually-permissible steps."),
        framework="FastAPI + LLM judge", model_id=JUDGE_MODEL,
        domain="Governance",
        data_access=["agent_traces", "evaluation_audit"],
        escalation="Detection to Sentinel ALERT",
        allowed_actions=["correlate_agent_actions", "detect_authority_stacking", "submit_finding"],
        prohibited_actions=["block_transaction", "modify_policy", "issue_verdict"],
        max_authority="advisory",
    ),
}

# (kill_switch, hitl_mode) per platform agent. The gate is the enforcement point,
# so it carries no kill switch of its own; the LLM-backed advisors do.
_FLAGS = {
    "gate":                 (0, "none"),
    "sentinel":             (1, "oversight"),
    "coordinator":          (1, "oversight"),
    "policy-reviewer":      (1, "none"),
    "structuring-detector": (1, "none"),
    "conflict-detector":    (1, "none"),
    "collusion-detector":   (1, "none"),
}


def seed(db_path: str) -> None:
    if not os.path.exists(db_path):
        print(f"  SKIP (missing): {db_path}")
        return
    db = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(config_agent_cards)")}
        if not cols:
            print(f"  SKIP (no config_agent_cards — run seed_agent_cards.py first): {db_path}")
            return
        has_flags = {"kill_switch", "hitl_mode", "hitl_escalation"} <= cols

        for agent_id, m in CARDS.items():
            ks, mode = _FLAGS.get(agent_id, (0, "none"))
            if has_flags:
                db.execute(
                    """
                    INSERT INTO config_agent_cards
                        (app_id, agent_id, name, agent_type, capability, purpose, framework,
                         model_id, domain, data_access, escalation, allowed_actions,
                         prohibited_actions, max_authority, kill_switch, hitl_escalation,
                         hitl_mode, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
                    ON CONFLICT(app_id, agent_id) DO UPDATE SET
                        name=excluded.name, agent_type=excluded.agent_type,
                        capability=excluded.capability, purpose=excluded.purpose,
                        framework=excluded.framework, model_id=excluded.model_id,
                        domain=excluded.domain, data_access=excluded.data_access,
                        escalation=excluded.escalation, allowed_actions=excluded.allowed_actions,
                        prohibited_actions=excluded.prohibited_actions,
                        max_authority=excluded.max_authority, updated_at=datetime('now')
                    """,
                    (
                        APP_ID, agent_id, m["name"], m["agent_type"], m["capability"],
                        m["purpose"], m["framework"], m["model_id"], m["domain"],
                        json.dumps(m["data_access"]), m["escalation"],
                        json.dumps(m["allowed_actions"]), json.dumps(m["prohibited_actions"]),
                        m["max_authority"], ks, 1 if mode != "none" else 0, mode,
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT INTO config_agent_cards
                        (app_id, agent_id, name, agent_type, capability, purpose, framework,
                         model_id, domain, data_access, escalation, allowed_actions,
                         prohibited_actions, max_authority, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
                    ON CONFLICT(app_id, agent_id) DO UPDATE SET
                        name=excluded.name, agent_type=excluded.agent_type,
                        capability=excluded.capability, purpose=excluded.purpose,
                        framework=excluded.framework, model_id=excluded.model_id,
                        domain=excluded.domain, data_access=excluded.data_access,
                        escalation=excluded.escalation, allowed_actions=excluded.allowed_actions,
                        prohibited_actions=excluded.prohibited_actions,
                        max_authority=excluded.max_authority, updated_at=datetime('now')
                    """,
                    (
                        APP_ID, agent_id, m["name"], m["agent_type"], m["capability"],
                        m["purpose"], m["framework"], m["model_id"], m["domain"],
                        json.dumps(m["data_access"]), m["escalation"],
                        json.dumps(m["allowed_actions"]), json.dumps(m["prohibited_actions"]),
                        m["max_authority"],
                    ),
                )
        db.commit()
        n = db.execute("SELECT count(*) FROM config_agent_cards WHERE app_id=?",
                       (APP_ID,)).fetchone()[0]
        print(f"  OK: {db_path} - {n} cards for app '{APP_ID}'")
    finally:
        db.close()


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    targets = sys.argv[1:] or [
        os.path.join(here, "pap.db"),       # live
        os.path.join(here, "pap.seed.db"),  # seed
    ]
    print("Seeding config_agent_cards (governance plane):")
    for t in targets:
        seed(t)
    print("Done.")
