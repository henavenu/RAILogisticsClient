"""Add governance_alerts table and zero_tolerance column

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-15

Governance Alerts — unified, tiered alert store replacing the two-table
(platform_alerts + trust_score_alerts) approach for application governance.

Design decisions confirmed:
  • Two-source model: Sentinel duo (primary) + PE zero-tolerance safety net
  • Async coordinator wiring: Sentinel writes provisional alert → Coordinator
    enriches with action/target/itsm within seconds
  • Deduplication: one POLICY_VIOLATION per session per dimension (recurrence_count)
  • Existing platform_alerts + trust_score_alerts kept as historical data
  • New alerts go exclusively to governance_alerts from this migration onward

Tier values
───────────
  INCIDENT        Multi-turn / compound pattern actively unfolding
                  Source: Sentinel ALERT → Coordinator Tier 1 or 2
  HITL            Human decision gate — blocks or requires review
                  Source: PE HITL pack trigger, Coordinator Tier 1
  POLICY_VIOLATION Single evaluation threshold breach (zero-tolerance dims)
                  Source: PE COERCIVE verdict on zero-tolerance dimension
  RELIABILITY     Platform health — governance quality degraded
                  Source: SRE burn-rate breach, provider unavailable
  ADVISORY        Pattern detected, tuning recommended, no immediate action
                  Source: Sentinel WATCH, Coordinator Tier 3/4

Zero-tolerance dimensions (PE raises POLICY_VIOLATION immediately on COERCIVE)
───────────────────────────────────────────────────────────────────────────────
  sensitive_data_disclosure    sanctions_country_reference
  sanctions_entity_mention     prompt_injection
  jailbreak                    hate
  violence                     self_harm

All other dimensions are rolled up by the Sentinel window before alerting.
"""
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision      = "0015"
down_revision = "0014"
branch_labels = None
depends_on    = None

# Dimensions that raise a POLICY_VIOLATION alert on their first COERCIVE verdict.
# All others defer to the Sentinel window for cross-turn pattern analysis.
_ZERO_TOLERANCE = [
    "sensitive_data_disclosure",
    "sanctions_country_reference",
    "sanctions_entity_mention",
    "prompt_injection",
    "jailbreak",
    "hate",
    "violence",
    "self_harm",
]


def upgrade() -> None:
    # ── 1. New governance_alerts table ────────────────────────────────────────
    op.create_table(
        "governance_alerts",

        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),

        # ── Classification ────────────────────────────────────────────────────
        # INCIDENT | HITL | POLICY_VIOLATION | RELIABILITY | ADVISORY
        sa.Column("tier",     sa.String(32), nullable=False),
        # URGENT | HIGH | MEDIUM | LOW
        sa.Column("severity", sa.String(16), nullable=False, server_default="HIGH"),

        # ── Actor attribution ─────────────────────────────────────────────────
        # sentinel | policy_engine | sre_metrics | guardrail_service | coordinator
        sa.Column("raiser",        sa.String(64),  nullable=False),
        # e.g. "sentinel_pattern:dimensional_escalation", "pe_pack:mandatory_safety"
        sa.Column("raiser_detail", sa.String(256), nullable=True),
        # user | agent | session | system
        sa.Column("caller_type",   sa.String(32),  nullable=True),
        # session_id / agent_name / app_id / provider_name
        sa.Column("caller_id",     sa.String(256), nullable=True),
        sa.Column("app_id",        sa.String(128), nullable=True),

        # ── Reason & condition ────────────────────────────────────────────────
        # dimensional_escalation | burn_rate_breach | hitl_trigger |
        # coercive_verdict | structuring | provider_unavailable | ...
        sa.Column("reason_code", sa.String(64), nullable=True),
        # One-sentence human-readable reason
        sa.Column("reason_text", sa.Text(),     nullable=True),
        # The exact signal that fired: "sensitive_data_disclosure score=8 ≥ threshold 7"
        sa.Column("condition",   sa.Text(),     nullable=True),
        # Which RAI dimension triggered this alert
        sa.Column("dimension",   sa.String(64), nullable=True),
        # Policy pack that fired
        sa.Column("pack_id",     sa.String(64), nullable=True),

        # ── Evidence links ────────────────────────────────────────────────────
        sa.Column("interaction_id", sa.String(256), nullable=True),
        sa.Column("session_id",     sa.String(256), nullable=True),
        # [] of turn numbers where key violations occurred (from Sentinel)
        sa.Column("evidence_turns", sa.JSON(),      nullable=True),

        # ── Coordinator recommendation ─────────────────────────────────────────
        # Written asynchronously once Coordinator analysis completes.
        # Null until Coordinator enrichment fires (provisional alert state).
        sa.Column("coord_action", sa.Text(),     nullable=True),
        # OPERATOR | COMPLIANCE | MLRO | SYSTEM | AGENT_TEAM | ML_OPS
        sa.Column("coord_target", sa.String(64), nullable=True),
        # {type, urgency, assignment_group, short_description}
        sa.Column("coord_itsm",   sa.JSON(),     nullable=True),

        # ── Deduplication ─────────────────────────────────────────────────────
        # Policy Violation: one per session+dimension; subsequent hits increment this.
        sa.Column("recurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_seen_at",     sa.DateTime(), nullable=True),

        # ── Payload — structured context for the reviewer ─────────────────────
        sa.Column("payload", sa.JSON(), nullable=True),

        # ── Lifecycle ─────────────────────────────────────────────────────────
        # open | acknowledged | in_review | resolved | suppressed | false_positive
        sa.Column("status",           sa.String(32),  nullable=False, server_default="open"),
        sa.Column("triggered_at",     sa.DateTime(),  nullable=False),
        sa.Column("acknowledged_at",  sa.DateTime(),  nullable=True),
        sa.Column("acknowledged_by",  sa.String(128), nullable=True),
        sa.Column("resolved_at",      sa.DateTime(),  nullable=True),
        sa.Column("resolved_by",      sa.String(128), nullable=True),
        # acknowledged | override | escalated | auto_resolved | false_positive
        sa.Column("resolution",       sa.String(64),  nullable=True),
        sa.Column("resolution_note",  sa.Text(),      nullable=True),
        sa.Column("created_at",       sa.DateTime(),  nullable=True),
        sa.Column("updated_at",       sa.DateTime(),  nullable=True),

        sa.PrimaryKeyConstraint("id"),
    )

    # Indexes for the most common query patterns
    op.create_index("ix_gov_alerts_tier",           "governance_alerts", ["tier"])
    op.create_index("ix_gov_alerts_status",         "governance_alerts", ["status"])
    op.create_index("ix_gov_alerts_app_id",         "governance_alerts", ["app_id"])
    op.create_index("ix_gov_alerts_session_id",     "governance_alerts", ["session_id"])
    op.create_index("ix_gov_alerts_triggered_at",   "governance_alerts", ["triggered_at"])
    op.create_index("ix_gov_alerts_dimension",      "governance_alerts", ["dimension"])
    op.create_index("ix_gov_alerts_interaction_id", "governance_alerts", ["interaction_id"])

    # ── 2. Add zero_tolerance to config_evaluation_dimensions ─────────────────
    op.add_column(
        "config_evaluation_dimensions",
        sa.Column("zero_tolerance", sa.Boolean(), nullable=False, server_default="0"),
    )

    # Mark the critical dimensions
    ids = ", ".join(f"'{d}'" for d in _ZERO_TOLERANCE)
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    zero_tolerance = 1
        WHERE  dimension_id IN ({ids})
    """)


def downgrade() -> None:
    op.drop_index("ix_gov_alerts_interaction_id", table_name="governance_alerts")
    op.drop_index("ix_gov_alerts_dimension",      table_name="governance_alerts")
    op.drop_index("ix_gov_alerts_triggered_at",   table_name="governance_alerts")
    op.drop_index("ix_gov_alerts_session_id",     table_name="governance_alerts")
    op.drop_index("ix_gov_alerts_app_id",         table_name="governance_alerts")
    op.drop_index("ix_gov_alerts_status",         table_name="governance_alerts")
    op.drop_index("ix_gov_alerts_tier",           table_name="governance_alerts")
    op.drop_table("governance_alerts")
    op.drop_column("config_evaluation_dimensions", "zero_tolerance")
