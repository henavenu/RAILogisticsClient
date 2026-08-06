"""
Seed config_agent_cards for the demo.

One row per MyBank agent the registry shows. agent_id MUST equal
config_app_agents.agent_name (tools + invokes/a2a come LIVE from there). Runtime
fields (status, eval/violation/breach counts, trust score, cost) are computed by
the API from audit_evaluation_audit — never stored here.

Field provenance:
  • LIVE (config_app_agents):   tools, invokes (a2a_targets), status, declared
  • COMPUTED (audit):           trust score, evaluations, P/N/C, violations, cost
  • OPERATOR-AUTHORED (here):    name, type, capability, purpose, domain,
                                data_access, escalation, allowed/prohibited actions,
                                max_authority
(owner_team and risk_tier were dropped — operator fields not in the attribute spec.)

Idempotent: creates/migrates the table and upserts on (app_id, agent_id).
"""
import json
import os
import sqlite3
import sys

APP_ID = "mybank"
MODEL_ID = "llama-3.1-8b-instant"
FRAMEWORK = "LangGraph"

# agent_id toseeded metadata. agent_id matches config_app_agents.agent_name exactly.
CARDS = {
    "coordinator": dict(
        name="Fraud Review Coordinator", agent_type="router", capability="orchestration",
        purpose=("Supervises the MyBank agent graph - routes each customer request to the "
                 "right specialist, aggregates their findings, and escalates unresolved "
                 "disputes (such as a deadlocked card-denial review) for policy adjudication."),
        domain="Finance",
        data_access=["customer_profile", "transaction_history"],
        escalation="Policy review to Human review queue (PAP)",
        allowed_actions=["route_request", "aggregate_findings", "request_policy_review", "handoff_to_agent"],
        prohibited_actions=["modify_account", "authorize_payment", "override_policy_verdict"],
        max_authority="orchestrate",
    ),
    "cx_agent": dict(
        name="Customer Experience Agent", agent_type="llm", capability="advisory only",
        purpose=("Represents the customer-impact lens in disputes - assesses fairness, "
                 "hardship, and relationship value when a transaction is declined or held."),
        domain="Finance",
        data_access=["customer_profile"],
        escalation="Coordinator to Human review queue",
        allowed_actions=["review_customer_impact", "submit_recommendation"],
        prohibited_actions=["authorize_payment", "reverse_decline", "access_card_pan"],
        max_authority="advisory",
    ),
    "intl_wire_agent": dict(
        name="International Wire Agent", agent_type="llm", capability="execute (governed)",
        purpose=("Handles cross-border wire requests - screens against sanctions and "
                 "structuring policy before initiating an international transfer."),
        domain="Finance",
        data_access=["account_balance", "sanctions_lists", "transaction_history"],
        escalation="Policy Council to Human review queue",
        allowed_actions=["screen_sanctions", "initiate_international_transfer", "submit_recommendation"],
        prohibited_actions=["bypass_sanctions_screening", "exceed_transfer_limit"],
        max_authority="execute",
    ),
    # "policy_council" card removed — it's a legacy council *label*, not a real single
    # agent. The fraud-review council is represented by the Fraud Review Coordinator +
    # the four reviewer agents (transaction_monitoring / revenue / cx / risk_policy).
    "revenue_agent": dict(
        name="Revenue Agent", agent_type="llm", capability="advisory only",
        purpose=("Weighs revenue and commercial-relationship impact in transaction disputes - "
                 "flags high-value customers and lifetime-value considerations."),
        domain="Finance",
        data_access=["customer_ltv", "transaction_history"],
        escalation="Coordinator to Human review queue",
        allowed_actions=["review_revenue_impact", "submit_recommendation"],
        prohibited_actions=["authorize_payment", "waive_fees", "override_risk_verdict"],
        max_authority="advisory",
    ),
    "risk_policy_agent": dict(
        name="Risk Policy Agent", agent_type="llm", capability="advisory only",
        purpose=("Applies the risk and fraud-policy lens - evaluates exposure, fraud signals, "
                 "and regulatory risk on declined or held transactions."),
        domain="Finance",
        data_access=["risk_signals", "transaction_history"],
        escalation="Policy Council to Human review queue",
        allowed_actions=["review_risk_exposure", "flag_fraud_signal", "submit_recommendation"],
        prohibited_actions=["authorize_payment", "clear_fraud_hold_unilaterally"],
        max_authority="advisory",
    ),
    "transaction_monitoring": dict(
        name="Transaction Monitoring Agent", agent_type="llm", capability="advisory only",
        purpose=("Runs the AML / transaction-monitoring lens - detects structuring, velocity "
                 "anomalies, and suspicious patterns across the customer's recent activity."),
        domain="Finance",
        data_access=["transaction_history", "aml_watchlists"],
        escalation="Policy Council to Human review queue",
        allowed_actions=["review_transaction_patterns", "flag_structuring", "submit_recommendation"],
        prohibited_actions=["authorize_payment", "close_sar_unilaterally"],
        max_authority="advisory",
    ),
    # ── Observed agents (run by the app, not formally declared) — #2: be thorough ──
    "account": dict(
        name="Account Agent", agent_type="llm", capability="advisory only",
        purpose="Resolves account-balance and standing for customer queries and downstream reviewers.",
        domain="Finance",
        data_access=["account_balance", "customer_profile"],
        escalation="Coordinator to Human review queue",
        allowed_actions=["read_account", "submit_recommendation"],
        prohibited_actions=["modify_account", "authorize_payment"],
        max_authority="advisory",
    ),
    "domestic_agent": dict(
        name="Domestic Transfer Agent", agent_type="llm", capability="execute (governed)",
        purpose="Handles domestic transfer requests, screening for fraud and limit breaches before execution.",
        domain="Finance",
        data_access=["account_balance", "transaction_history"],
        escalation="Risk Policy Agent to Human review queue",
        allowed_actions=["screen_fraud", "initiate_domestic_transfer", "submit_recommendation"],
        prohibited_actions=["exceed_transfer_limit", "bypass_fraud_check"],
        max_authority="execute",
    ),
    "profile": dict(
        name="Customer Profile Agent", agent_type="llm", capability="advisory only",
        purpose="Resolves customer profile and KYC attributes to contextualise a request for the graph.",
        domain="Finance",
        data_access=["customer_profile", "kyc_attributes"],
        escalation="Coordinator to Human review queue",
        allowed_actions=["read_profile", "submit_recommendation"],
        prohibited_actions=["modify_profile", "access_card_pan"],
        max_authority="advisory",
    ),
}

DDL = """
CREATE TABLE IF NOT EXISTS config_agent_cards (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id              VARCHAR(128) NOT NULL,
    agent_id            VARCHAR(128) NOT NULL,
    name                VARCHAR(128) NOT NULL,
    agent_type          VARCHAR(32)  NOT NULL DEFAULT 'llm',
    capability          VARCHAR(64),
    purpose             TEXT,
    framework           VARCHAR(48)  NOT NULL DEFAULT 'LangGraph',
    model_id            VARCHAR(64),
    domain              VARCHAR(48),
    data_access         JSON,
    escalation          VARCHAR(160),
    allowed_actions     JSON,
    prohibited_actions  JSON,
    max_authority       VARCHAR(32),
    kill_switch         INTEGER NOT NULL DEFAULT 0,
    hitl_escalation     INTEGER NOT NULL DEFAULT 0,
    hitl_mode           TEXT NOT NULL DEFAULT 'none',
    created_at          DATETIME,
    updated_at          DATETIME,
    CONSTRAINT uq_agent_card UNIQUE (app_id, agent_id)
);
CREATE INDEX IF NOT EXISTS ix_config_agent_cards_app_id   ON config_agent_cards (app_id);
CREATE INDEX IF NOT EXISTS ix_config_agent_cards_agent_id ON config_agent_cards (agent_id);
"""

# Columns added after the original table shipped — ALTER existing tables in place.
_MIGRATE = [
    ("domain",      "VARCHAR(48)"),
    ("data_access", "JSON"),
    ("escalation",  "VARCHAR(160)"),
    ("kill_switch",     "INTEGER NOT NULL DEFAULT 0"),
    ("hitl_escalation", "INTEGER NOT NULL DEFAULT 0"),
    ("hitl_mode",       "TEXT NOT NULL DEFAULT 'none'"),
]

# Governance config-flag defaults per agent: (kill_switch, hitl_mode). hitl_mode ∈
# {none, by_design, human, agent} — who is in the loop on escalation:
#   none      — no checkpoint
#   by_design — human checkpoints built into the agent's own workflow (was 'inherent')
#   human     — the platform can pause it for a human reviewer       (was 'oversight')
#   agent     — oversight from a Platform Agent (e.g. Sentinel), A2A  (new)
# A deliberate mix across the orchestrator, money-movement sub-agents, and standalone
# observed agents so the demo shows every mode. Agents not listed → (0, 'none').
_FLAGS = {
    "coordinator":            (1, "agent"),      # orchestrator overseen by a Platform Agent
    "intl_wire_agent":        (1, "human"),      # cross-border money movement → human reviewer
    "domestic_agent":         (1, "human"),      # domestic money movement → human reviewer
    "transaction_monitoring": (1, "agent"),      # AML lens overseen by a Platform Agent
    "risk_policy_agent":      (1, "agent"),      # risk lens overseen by a Platform Agent
    "cx_agent":               (0, "by_design"),  # advisory, built-in fairness checkpoint
    "account":                (0, "by_design"),  # standalone, built-in checks
}


def seed(db_path: str) -> None:
    if not os.path.exists(db_path):
        print(f"  SKIP (missing): {db_path}")
        return
    db = sqlite3.connect(db_path)
    try:
        db.executescript(DDL)
        existing = {r[1] for r in db.execute("PRAGMA table_info(config_agent_cards)")}
        _added = set()
        for col, decl in _MIGRATE:
            if col not in existing:
                db.execute(f"ALTER TABLE config_agent_cards ADD COLUMN {col} {decl}")
                _added.add(col)
        # One-time backfill: when the governance-flag columns are FIRST added, apply the
        # per-agent _FLAGS defaults to existing rows. The INSERT below is insert-only for
        # these flags (not in ON CONFLICT DO UPDATE), so it never clobbers later operator
        # toggles — this backfill runs once per DB, only on the migration that adds them.
        if "kill_switch" in _added or "hitl_mode" in _added or "hitl_escalation" in _added:
            for agent_id, (ks, mode) in _FLAGS.items():
                db.execute(
                    "UPDATE config_agent_cards SET kill_switch=?, hitl_mode=?, hitl_escalation=? "
                    "WHERE app_id=? AND agent_id=?",
                    (ks, mode, 1 if mode != "none" else 0, APP_ID, agent_id),
                )
        # One-time HITL-vocabulary migration: relabel the legacy taxonomy to the new one
        # (oversight→human, inherent→by_design, both→agent). Gated on the presence of any
        # legacy value, so it runs exactly once per DB and a later operator toggle (which
        # uses the new vocabulary) is never clobbered on subsequent re-seeds.
        _legacy = db.execute(
            "SELECT COUNT(*) FROM config_agent_cards WHERE hitl_mode IN ('oversight','inherent','both')"
        ).fetchone()[0]
        if _legacy:
            for _old, _new in (("oversight", "human"), ("inherent", "by_design"), ("both", "agent")):
                db.execute("UPDATE config_agent_cards SET hitl_mode=? WHERE hitl_mode=?", (_new, _old))
            # Re-assert the curated demo spread now the taxonomy changed (same one-time gate).
            for agent_id, (ks, mode) in _FLAGS.items():
                db.execute(
                    "UPDATE config_agent_cards SET kill_switch=?, hitl_mode=?, hitl_escalation=? "
                    "WHERE app_id=? AND agent_id=?",
                    (ks, mode, 1 if mode != "none" else 0, APP_ID, agent_id),
                )
        for agent_id, m in CARDS.items():
            _ks, _mode = _FLAGS.get(agent_id, (0, "none"))
            db.execute(
                """
                INSERT INTO config_agent_cards
                    (app_id, agent_id, name, agent_type, capability, purpose, framework,
                     model_id, domain, data_access, escalation, allowed_actions,
                     prohibited_actions, max_authority, kill_switch, hitl_escalation, hitl_mode,
                     created_at, updated_at)
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
                    m["purpose"], FRAMEWORK, MODEL_ID, m["domain"],
                    json.dumps(m["data_access"]), m["escalation"],
                    json.dumps(m["allowed_actions"]), json.dumps(m["prohibited_actions"]),
                    m["max_authority"], _ks, 1 if _mode != "none" else 0, _mode,
                ),
            )
        db.commit()
        n = db.execute("SELECT count(*) FROM config_agent_cards WHERE app_id=?", (APP_ID,)).fetchone()[0]
        print(f"  OK: {db_path} - {n} cards for app '{APP_ID}'")
    finally:
        db.close()


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    targets = sys.argv[1:] or [
        os.path.join(here, "pap.db"),       # live
        os.path.join(here, "pap.seed.db"),  # seed
    ]
    print("Seeding config_agent_cards:")
    for t in targets:
        seed(t)
    print("Done.")
