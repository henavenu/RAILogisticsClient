"""Expand schema — T2/T3/COGS/audit additions

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-22

Changes
-------
Existing table modifications:
  audit_evaluation_audit      — add enterprise_id, model_id, input_tokens, output_tokens
  audit_evaluation_dimensions — add score_normalized, is_violation, policy_id

New tables (in dependency order):
  audit_log                   — flat, denormalized export-friendly compliance copy
  tier2_eval_scores           — Phoenix/Arize annotation schema proxy (T2)
  config_app_trust_weights    — per-app T3 sub-score weighting formula
  trust_score_snapshots       — time-bucketed composite trust scores (T3 compute job)
  trust_score_alerts          — SRE SLO burn-rate breach records
  agent_trust_scores          — rolling per-agent trust score
  config_model_pricing        — LLM pricing reference data (COGS)
  cogs_records                — per-evaluation cost records (COGS)
  config_policy_templates     — named policy presets / seed library
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── Modify: audit_evaluation_audit ────────────────────────────────────────
    # render_as_batch=True (set in env.py) handles SQLite's lack of native
    # ALTER TABLE support by recreating the table transparently.
    with op.batch_alter_table("audit_evaluation_audit") as batch_op:
        batch_op.add_column(sa.Column("enterprise_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("model_id",      sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("input_tokens",  sa.Integer,     nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer,     nullable=True))
        batch_op.create_index("ix_eval_audit_enterprise_id", ["enterprise_id"])

    # ── Modify: audit_evaluation_dimensions ───────────────────────────────────
    with op.batch_alter_table("audit_evaluation_dimensions") as batch_op:
        batch_op.add_column(sa.Column("score_normalized", sa.Float,   nullable=True))
        batch_op.add_column(sa.Column("is_violation",     sa.Boolean, nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("policy_id",        sa.Integer, nullable=True))

    # ── New: audit_log ────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id",                 sa.Integer,    primary_key=True, autoincrement=True),
        # Correlation
        sa.Column("interaction_id",     sa.String(36),  nullable=False, unique=True),
        sa.Column("request_id",         sa.String(128), nullable=True),
        sa.Column("enterprise_id",      sa.String(128), nullable=True),
        sa.Column("app_id",             sa.String(128), nullable=True),
        sa.Column("agent_id",           sa.String(128), nullable=True),
        sa.Column("session_id",         sa.String(128), nullable=True),
        sa.Column("turn_number",        sa.Integer,     nullable=True),
        # Evaluation context
        sa.Column("role",               sa.String(32),  nullable=False),
        sa.Column("model_id",           sa.String(128), nullable=True),
        # Verdict
        sa.Column("verdict",            sa.String(16),  nullable=False),
        sa.Column("session_verdict",    sa.String(16),  nullable=True),
        sa.Column("session_escalated",  sa.Boolean,     nullable=False, server_default=sa.false()),
        sa.Column("enforcement_action", sa.String(16),  nullable=True),
        # Dimension detail (flat JSON — no join needed for export)
        sa.Column("dimensions_json",    sa.JSON,        nullable=True),
        sa.Column("violations_json",    sa.JSON,        nullable=True),
        sa.Column("remediations_json",  sa.JSON,        nullable=True),
        # Performance
        sa.Column("latency_ms",         sa.Float,       nullable=True),
        # COGS (pre-computed at write time)
        sa.Column("input_tokens",       sa.Integer,     nullable=True),
        sa.Column("output_tokens",      sa.Integer,     nullable=True),
        sa.Column("estimated_cost_usd", sa.Float,       nullable=True),
        sa.Column("fail_reason",        sa.String(64),  nullable=True),
        sa.Column("timestamp",          sa.DateTime,    nullable=False),
        sa.Column("exported_at",        sa.DateTime,    nullable=True),
    )
    op.create_index("ix_audit_log_interaction_id",  "audit_log", ["interaction_id"])
    op.create_index("ix_audit_log_enterprise_id",   "audit_log", ["enterprise_id"])
    op.create_index("ix_audit_log_app_id",          "audit_log", ["app_id"])
    op.create_index("ix_audit_log_verdict",         "audit_log", ["verdict"])
    op.create_index("ix_audit_log_timestamp",       "audit_log", ["timestamp"])

    # ── New: tier2_eval_scores ────────────────────────────────────────────────
    op.create_table(
        "tier2_eval_scores",
        sa.Column("id",             sa.Integer,    primary_key=True, autoincrement=True),
        # Correlation to T1 evaluation
        sa.Column("interaction_id", sa.String(36),  nullable=False),
        sa.Column("app_id",         sa.String(128), nullable=False),
        sa.Column("enterprise_id",  sa.String(128), nullable=True),
        sa.Column("agent_id",       sa.String(128), nullable=True),
        # Phoenix/Arize annotation fields
        sa.Column("eval_name",      sa.String(128), nullable=False),
        sa.Column("score",          sa.Float,       nullable=True),   # 0.0–1.0
        sa.Column("label",          sa.String(64),  nullable=True),   # PASS | FAIL | NEEDS_REVIEW
        sa.Column("explanation",    sa.Text,        nullable=True),
        # Provenance
        sa.Column("annotator_kind", sa.String(32),  nullable=False, server_default="CODE"),
        sa.Column("provider_id",    sa.String(64),  nullable=False, server_default="mock"),
        sa.Column("evaluated_at",   sa.DateTime,    nullable=False),
        sa.Column("created_at",     sa.DateTime,    nullable=False),
    )
    op.create_index("ix_t2_interaction_id", "tier2_eval_scores", ["interaction_id"])
    op.create_index("ix_t2_app_id",         "tier2_eval_scores", ["app_id"])
    op.create_index("ix_t2_eval_name",      "tier2_eval_scores", ["eval_name"])
    op.create_index("ix_t2_evaluated_at",   "tier2_eval_scores", ["evaluated_at"])

    # ── New: config_app_trust_weights ─────────────────────────────────────────
    op.create_table(
        "config_app_trust_weights",
        sa.Column("id",                  sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("enterprise_id",       sa.String(128), nullable=True),
        sa.Column("app_id",              sa.String(128), nullable=True),  # NULL = enterprise default
        # Sub-score weights — must sum to 1.0
        sa.Column("weight_safety",       sa.Float, nullable=False, server_default="0.30"),
        sa.Column("weight_security",     sa.Float, nullable=False, server_default="0.25"),
        sa.Column("weight_compliance",   sa.Float, nullable=False, server_default="0.20"),
        sa.Column("weight_operational",  sa.Float, nullable=False, server_default="0.10"),
        sa.Column("weight_quality",      sa.Float, nullable=False, server_default="0.15"),
        # Burn rate alert thresholds
        sa.Column("alert_threshold_1h",  sa.Float, nullable=False, server_default="0.05"),
        sa.Column("alert_threshold_6h",  sa.Float, nullable=False, server_default="0.10"),
        sa.Column("alert_threshold_24h", sa.Float, nullable=False, server_default="0.15"),
        sa.Column("active",              sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at",          sa.DateTime, nullable=False),
        sa.Column("updated_at",          sa.DateTime, nullable=False),
        sa.UniqueConstraint("enterprise_id", "app_id", name="uq_trust_weights_app"),
    )

    # ── New: trust_score_snapshots ────────────────────────────────────────────
    op.create_table(
        "trust_score_snapshots",
        sa.Column("id",                sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("enterprise_id",     sa.String(128), nullable=True),
        sa.Column("app_id",            sa.String(128), nullable=True),   # NULL = enterprise roll-up
        sa.Column("agent_id",          sa.String(128), nullable=True),   # NULL = app-level
        # Time window
        sa.Column("window_type",       sa.String(8),  nullable=False),   # 1h | 6h | 24h | 7d | 30d
        sa.Column("window_start",      sa.DateTime,   nullable=False),
        sa.Column("window_end",        sa.DateTime,   nullable=False),
        # Scores
        sa.Column("trust_score",       sa.Float,      nullable=False),
        sa.Column("score_safety",      sa.Float,      nullable=True),
        sa.Column("score_security",    sa.Float,      nullable=True),
        sa.Column("score_compliance",  sa.Float,      nullable=True),
        sa.Column("score_operational", sa.Float,      nullable=True),
        sa.Column("score_quality",     sa.Float,      nullable=True),
        # Volume
        sa.Column("total_evaluations", sa.Integer,    nullable=False, server_default="0"),
        sa.Column("total_violations",  sa.Integer,    nullable=False, server_default="0"),
        sa.Column("violation_rate",    sa.Float,      nullable=True),
        # Burn rate
        sa.Column("score_delta",       sa.Float,      nullable=True),
        sa.Column("burn_rate",         sa.Float,      nullable=True),
        sa.Column("computed_at",       sa.DateTime,   nullable=False),
    )
    op.create_index("ix_snapshot_app_window",  "trust_score_snapshots", ["app_id", "window_type", "window_start"])
    op.create_index("ix_snapshot_enterprise",  "trust_score_snapshots", ["enterprise_id", "window_type", "window_start"])
    op.create_index("ix_snapshot_computed_at", "trust_score_snapshots", ["computed_at"])

    # ── New: trust_score_alerts ───────────────────────────────────────────────
    op.create_table(
        "trust_score_alerts",
        sa.Column("id",                   sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("enterprise_id",        sa.String(128), nullable=True),
        sa.Column("app_id",               sa.String(128), nullable=True),
        sa.Column("agent_id",             sa.String(128), nullable=True),
        sa.Column("alert_type",           sa.String(32),  nullable=False),
        sa.Column("severity",             sa.String(16),  nullable=False),
        # Burn rate detail
        sa.Column("fast_window",          sa.String(8),   nullable=True),
        sa.Column("medium_window",        sa.String(8),   nullable=True),
        sa.Column("fast_burn_rate",       sa.Float,       nullable=True),
        sa.Column("medium_burn_rate",     sa.Float,       nullable=True),
        sa.Column("trust_score_at_alert", sa.Float,       nullable=True),
        sa.Column("dimension_id",         sa.String(64),  nullable=True),
        # Resolution
        sa.Column("status",               sa.String(16),  nullable=False, server_default="open"),
        sa.Column("triggered_at",         sa.DateTime,    nullable=False),
        sa.Column("acknowledged_at",      sa.DateTime,    nullable=True),
        sa.Column("resolved_at",          sa.DateTime,    nullable=True),
        sa.Column("notes",                sa.Text,        nullable=True),
    )
    op.create_index("ix_alert_app_triggered", "trust_score_alerts", ["app_id", "triggered_at"])
    op.create_index("ix_alert_status",        "trust_score_alerts", ["status"])

    # ── New: agent_trust_scores ───────────────────────────────────────────────
    op.create_table(
        "agent_trust_scores",
        sa.Column("id",                sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("enterprise_id",     sa.String(128), nullable=True),
        sa.Column("app_id",            sa.String(128), nullable=False),
        sa.Column("agent_id",          sa.String(128), nullable=False),
        sa.Column("trust_score",       sa.Float,       nullable=False, server_default="1.0"),
        sa.Column("score_safety",      sa.Float,       nullable=True),
        sa.Column("score_security",    sa.Float,       nullable=True),
        sa.Column("score_compliance",  sa.Float,       nullable=True),
        sa.Column("total_evaluations", sa.Integer,     nullable=False, server_default="0"),
        sa.Column("total_violations",  sa.Integer,     nullable=False, server_default="0"),
        sa.Column("last_violation_at", sa.DateTime,    nullable=True),
        sa.Column("first_seen_at",     sa.DateTime,    nullable=False),
        sa.Column("last_updated_at",   sa.DateTime,    nullable=False),
        sa.UniqueConstraint("enterprise_id", "app_id", "agent_id", name="uq_agent_trust"),
    )
    op.create_index("ix_agent_trust_app_id",   "agent_trust_scores", ["app_id"])
    op.create_index("ix_agent_trust_agent_id", "agent_trust_scores", ["agent_id"])

    # ── New: config_model_pricing ─────────────────────────────────────────────
    op.create_table(
        "config_model_pricing",
        sa.Column("id",                 sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("provider",           sa.String(64),  nullable=False),
        sa.Column("model_id",           sa.String(128), nullable=False),
        sa.Column("cost_per_1m_input",  sa.Float,       nullable=False),
        sa.Column("cost_per_1m_output", sa.Float,       nullable=False),
        sa.Column("effective_from",     sa.DateTime,    nullable=False),
        sa.Column("effective_until",    sa.DateTime,    nullable=True),  # NULL = current price
        sa.Column("active",             sa.Boolean,     nullable=False, server_default=sa.true()),
        sa.Column("created_at",         sa.DateTime,    nullable=False),
        sa.UniqueConstraint("provider", "model_id", "effective_from", name="uq_model_pricing"),
    )
    op.create_index("ix_model_pricing_model_id", "config_model_pricing", ["model_id"])

    # ── New: cogs_records ─────────────────────────────────────────────────────
    op.create_table(
        "cogs_records",
        sa.Column("id",                  sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("interaction_id",      sa.String(36),  nullable=False),
        sa.Column("app_id",              sa.String(128), nullable=False),
        sa.Column("enterprise_id",       sa.String(128), nullable=True),
        sa.Column("provider",            sa.String(64),  nullable=False),
        sa.Column("model_id",            sa.String(128), nullable=False),
        sa.Column("input_tokens",        sa.Integer,     nullable=False, server_default="0"),
        sa.Column("output_tokens",       sa.Integer,     nullable=False, server_default="0"),
        sa.Column("cost_per_1m_input",   sa.Float,       nullable=True),
        sa.Column("cost_per_1m_output",  sa.Float,       nullable=True),
        sa.Column("total_cost_usd",      sa.Float,       nullable=False, server_default="0.0"),
        sa.Column("timestamp",           sa.DateTime,    nullable=False),
    )
    op.create_index("ix_cogs_app_id",        "cogs_records", ["app_id", "timestamp"])
    op.create_index("ix_cogs_interaction_id","cogs_records", ["interaction_id"])
    op.create_index("ix_cogs_model_id",      "cogs_records", ["model_id"])

    # ── New: config_policy_templates ──────────────────────────────────────────
    op.create_table(
        "config_policy_templates",
        sa.Column("id",                 sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("template_id",        sa.String(64),  nullable=False, unique=True),
        sa.Column("display_name",       sa.String(128), nullable=False),
        sa.Column("description",        sa.Text,        nullable=True),
        sa.Column("industry",           sa.String(64),  nullable=True),
        sa.Column("risk_tier",          sa.String(16),  nullable=True),
        sa.Column("dimension_policies", sa.JSON,        nullable=True),
        sa.Column("pack_ids",           sa.JSON,        nullable=True),
        sa.Column("is_seed",            sa.Boolean,     nullable=False, server_default=sa.true()),
        sa.Column("active",             sa.Boolean,     nullable=False, server_default=sa.true()),
        sa.Column("version",            sa.String(16),  nullable=False, server_default="1.0.0"),
        sa.Column("created_at",         sa.DateTime,    nullable=False),
        sa.Column("updated_at",         sa.DateTime,    nullable=False),
    )


def downgrade() -> None:
    # Drop new tables in reverse dependency order
    op.drop_table("config_policy_templates")
    op.drop_table("cogs_records")
    op.drop_table("config_model_pricing")
    op.drop_table("agent_trust_scores")
    op.drop_table("trust_score_alerts")
    op.drop_table("trust_score_snapshots")
    op.drop_table("config_app_trust_weights")
    op.drop_table("tier2_eval_scores")
    op.drop_table("audit_log")

    # Remove added columns from existing tables
    with op.batch_alter_table("audit_evaluation_dimensions") as batch_op:
        batch_op.drop_column("policy_id")
        batch_op.drop_column("is_violation")
        batch_op.drop_column("score_normalized")

    with op.batch_alter_table("audit_evaluation_audit") as batch_op:
        batch_op.drop_index("ix_eval_audit_enterprise_id")
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
        batch_op.drop_column("model_id")
        batch_op.drop_column("enterprise_id")
