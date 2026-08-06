"""Initial schema — all 12 entities

Revision ID: 0001
Revises:
Create Date: 2026-04-21

Creates the full RAI Trust Platform schema in dependency order:

  Config tier (PAP-owned):
    config_evaluation_dimensions
    config_threshold_profiles
    config_policy_packs
    config_pack_dimensions          (FK → dimensions, profiles, packs)
    config_apps
    config_app_pack_assignments     (FK → apps, packs)
    config_dimension_overrides      (FK → apps, packs, dimensions, profiles)
    config_policies                 (FK → dimensions, apps)
    config_session_policy           (singleton)

  Audit tier:
    audit_pap_log
    audit_evaluation_audit
    audit_evaluation_dimensions     (FK → audit_evaluation_audit)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── 1. config_evaluation_dimensions ───────────────────────────────────────
    op.create_table(
        "config_evaluation_dimensions",
        sa.Column("dimension_id",     sa.String(64),  primary_key=True),
        sa.Column("category",         sa.String(32),  nullable=False),
        sa.Column("display_name",     sa.String(128), nullable=False),
        sa.Column("description",      sa.Text,        nullable=True),
        sa.Column("evaluator_type",   sa.String(32),  nullable=False, server_default="gs_call"),
        sa.Column("provider",         sa.String(64),  nullable=False),
        sa.Column("status",           sa.String(16),  nullable=False, server_default="active"),
        sa.Column("supported_roles",  sa.JSON,        nullable=True),
        sa.Column("requires_context", sa.Boolean,     nullable=False, server_default=sa.false()),
        sa.Column("created_at",       sa.DateTime,    nullable=False),
        sa.Column("updated_at",       sa.DateTime,    nullable=False),
    )
    op.create_index("ix_eval_dim_category", "config_evaluation_dimensions", ["category"])

    # ── 2. config_threshold_profiles ──────────────────────────────────────────
    op.create_table(
        "config_threshold_profiles",
        sa.Column("profile_id",    sa.String(32), primary_key=True),
        sa.Column("permissive_lo", sa.Float,      nullable=False),
        sa.Column("permissive_hi", sa.Float,      nullable=False),
        sa.Column("normative_lo",  sa.Float,      nullable=False),
        sa.Column("normative_hi",  sa.Float,      nullable=False),
        sa.Column("coercive_lo",   sa.Float,      nullable=False),
        sa.Column("coercive_hi",   sa.Float,      nullable=False),
        sa.Column("description",   sa.Text,       nullable=True),
        sa.Column("created_at",    sa.DateTime,   nullable=False),
        sa.Column("updated_at",    sa.DateTime,   nullable=False),
    )

    # ── 3. config_policy_packs ────────────────────────────────────────────────
    op.create_table(
        "config_policy_packs",
        sa.Column("pack_id",             sa.String(64),  primary_key=True),
        sa.Column("display_name",        sa.String(128), nullable=False),
        sa.Column("description",         sa.Text,        nullable=True),
        sa.Column("layer",               sa.String(32),  nullable=False),
        sa.Column("enforcement",         sa.String(32),  nullable=False),
        sa.Column("mode",                sa.String(32),  nullable=False, server_default="collect-all"),
        sa.Column("combining_algorithm", sa.String(32),  nullable=False, server_default="worst_case"),
        sa.Column("hitl_trigger",        sa.Boolean,     nullable=False, server_default=sa.false()),
        sa.Column("owner",               sa.String(128), nullable=True),
        sa.Column("version",             sa.String(16),  nullable=False, server_default="1.0.0"),
        sa.Column("effective_date",      sa.String(16),  nullable=True),
        sa.Column("active",              sa.Boolean,     nullable=False, server_default=sa.true()),
        sa.Column("created_at",          sa.DateTime,    nullable=False),
        sa.Column("updated_at",          sa.DateTime,    nullable=False),
    )

    # ── 4. config_pack_dimensions (FK → dimensions, profiles, packs) ──────────
    op.create_table(
        "config_pack_dimensions",
        sa.Column("id",           sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("pack_id",      sa.String(64), sa.ForeignKey("config_policy_packs.pack_id",             ondelete="CASCADE"),  nullable=False),
        sa.Column("dimension_id", sa.String(64), sa.ForeignKey("config_evaluation_dimensions.dimension_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("profile_id",   sa.String(32), sa.ForeignKey("config_threshold_profiles.profile_id",     ondelete="RESTRICT"), nullable=False),
        sa.Column("severity",     sa.Integer,    nullable=False, server_default="2"),
        sa.Column("abort",        sa.Boolean,    nullable=False, server_default=sa.false()),
        sa.Column("active_roles", sa.JSON,       nullable=True),
        sa.Column("note",         sa.Text,       nullable=True),
        sa.Column("created_at",   sa.DateTime,   nullable=False),
        sa.Column("updated_at",   sa.DateTime,   nullable=False),
        sa.UniqueConstraint("pack_id", "dimension_id", name="uq_pack_dimension"),
    )
    op.create_index("ix_pack_dim_pack_id",      "config_pack_dimensions", ["pack_id"])
    op.create_index("ix_pack_dim_dimension_id", "config_pack_dimensions", ["dimension_id"])

    # ── 5. config_apps ────────────────────────────────────────────────────────
    op.create_table(
        "config_apps",
        sa.Column("app_id",             sa.String(128), primary_key=True),
        sa.Column("display_name",       sa.String(256), nullable=True),
        sa.Column("description",        sa.Text,        nullable=True),
        sa.Column("base_tier",          sa.String(16),  nullable=False, server_default="MEDIUM"),
        sa.Column("enforcement_mode",   sa.String(16),  nullable=False, server_default="BLOCK"),
        sa.Column("dynamic_evaluation", sa.Boolean,     nullable=False, server_default=sa.false()),
        sa.Column("owner",              sa.String(128), nullable=True),
        sa.Column("active",             sa.Boolean,     nullable=False, server_default=sa.true()),
        sa.Column("created_at",         sa.DateTime,    nullable=False),
        sa.Column("updated_at",         sa.DateTime,    nullable=False),
    )

    # ── 6. config_app_pack_assignments (FK → apps, packs) ────────────────────
    op.create_table(
        "config_app_pack_assignments",
        sa.Column("id",          sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("app_id",      sa.String(128), sa.ForeignKey("config_apps.app_id",          ondelete="CASCADE"), nullable=False),
        sa.Column("pack_id",     sa.String(64),  sa.ForeignKey("config_policy_packs.pack_id",  ondelete="CASCADE"), nullable=False),
        sa.Column("active",      sa.Boolean,    nullable=False, server_default=sa.true()),
        sa.Column("assigned_at", sa.DateTime,   nullable=False),
        sa.Column("assigned_by", sa.String(128), nullable=False, server_default="pap-admin"),
        sa.UniqueConstraint("app_id", "pack_id", name="uq_app_pack"),
    )
    op.create_index("ix_app_pack_app_id",  "config_app_pack_assignments", ["app_id"])
    op.create_index("ix_app_pack_pack_id", "config_app_pack_assignments", ["pack_id"])

    # ── 7. config_dimension_overrides (FK → apps, packs, dimensions, profiles) ─
    op.create_table(
        "config_dimension_overrides",
        sa.Column("id",           sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("app_id",       sa.String(128), sa.ForeignKey("config_apps.app_id",                          ondelete="CASCADE"),  nullable=False),
        sa.Column("pack_id",      sa.String(64),  sa.ForeignKey("config_policy_packs.pack_id",                 ondelete="CASCADE"),  nullable=False),
        sa.Column("dimension_id", sa.String(64),  sa.ForeignKey("config_evaluation_dimensions.dimension_id",   ondelete="RESTRICT"), nullable=False),
        sa.Column("profile_id",   sa.String(32),  sa.ForeignKey("config_threshold_profiles.profile_id",        ondelete="RESTRICT"), nullable=True),
        sa.Column("severity",     sa.Integer,    nullable=True),
        sa.Column("abort",        sa.Boolean,    nullable=True),
        sa.Column("active",       sa.Boolean,    nullable=False, server_default=sa.true()),
        sa.Column("note",         sa.Text,       nullable=True),
        sa.Column("created_at",   sa.DateTime,   nullable=False),
        sa.Column("updated_at",   sa.DateTime,   nullable=False),
        sa.UniqueConstraint("app_id", "pack_id", "dimension_id", name="uq_dim_override"),
    )
    op.create_index("ix_dim_override_app_id", "config_dimension_overrides", ["app_id"])

    # ── 8. config_policies (FK → dimensions, apps) ────────────────────────────
    op.create_table(
        "config_policies",
        sa.Column("id",                    sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("dimension",             sa.String(64), sa.ForeignKey("config_evaluation_dimensions.dimension_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("app_id",                sa.String(128), sa.ForeignKey("config_apps.app_id", ondelete="CASCADE"), nullable=True),
        sa.Column("severity",              sa.Integer,    nullable=False, server_default="1"),
        sa.Column("permissive_lo",         sa.Float,      nullable=False, server_default="1.0"),
        sa.Column("permissive_hi",         sa.Float,      nullable=False, server_default="4.0"),
        sa.Column("normative_lo",          sa.Float,      nullable=False, server_default="5.0"),
        sa.Column("normative_hi",          sa.Float,      nullable=False, server_default="6.0"),
        sa.Column("coercive_lo",           sa.Float,      nullable=False, server_default="7.0"),
        sa.Column("coercive_hi",           sa.Float,      nullable=False, server_default="9.0"),
        sa.Column("remediation_normative", sa.Text,       nullable=True),
        sa.Column("remediation_coercive",  sa.Text,       nullable=True),
        sa.Column("human_note",            sa.Text,       nullable=True),
        sa.Column("ops_alert",             sa.Boolean,    nullable=False, server_default=sa.false()),
        sa.Column("incident_routing",      sa.String(32), nullable=False, server_default="none"),
        sa.Column("active",                sa.Boolean,    nullable=False, server_default=sa.true()),
        sa.Column("version",               sa.Integer,    nullable=False, server_default="1"),
        sa.Column("published_at",          sa.DateTime,   nullable=True),
        sa.Column("created_at",            sa.DateTime,   nullable=False),
        sa.Column("updated_at",            sa.DateTime,   nullable=False),
        sa.UniqueConstraint("dimension", "app_id", name="uq_policy_dim_app"),
    )
    op.create_index("ix_config_policies_dimension", "config_policies", ["dimension"])
    op.create_index("ix_config_policies_app_id",    "config_policies", ["app_id"])

    # ── 9. config_session_policy (singleton row) ──────────────────────────────
    op.create_table(
        "config_session_policy",
        sa.Column("id",                         sa.Integer, primary_key=True),
        sa.Column("trust_delta_coercive",       sa.Float,   nullable=False, server_default="-0.20"),
        sa.Column("trust_delta_normative",      sa.Float,   nullable=False, server_default="-0.05"),
        sa.Column("trust_delta_permissive",     sa.Float,   nullable=False, server_default="0.02"),
        sa.Column("trust_floor",                sa.Float,   nullable=False, server_default="0.20"),
        sa.Column("trust_ceiling",              sa.Float,   nullable=False, server_default="1.0"),
        sa.Column("session_ttl_seconds",        sa.Integer, nullable=False, server_default="1800"),
        sa.Column("dedup_exact_block",          sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("dedup_similarity_threshold", sa.Float,   nullable=False, server_default="0.92"),
        sa.Column("dedup_window",               sa.Integer, nullable=False, server_default="50"),
        sa.Column("dedup_repetition_penalty",   sa.Float,   nullable=False, server_default="-0.05"),
        sa.Column("updated_at",                 sa.DateTime, nullable=False),
        sa.Column("updated_by",                 sa.String(128), nullable=False, server_default="system"),
    )

    # ── 10. audit_pap_log ─────────────────────────────────────────────────────
    op.create_table(
        "audit_pap_log",
        sa.Column("id",          sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id",   sa.String(128), nullable=False),
        sa.Column("app_id",      sa.String(128), nullable=True),
        sa.Column("action",      sa.String(16), nullable=False),
        sa.Column("actor",       sa.String(128), nullable=False, server_default="pap-admin"),
        sa.Column("old_values",  sa.Text,       nullable=True),
        sa.Column("new_values",  sa.Text,       nullable=True),
        sa.Column("notes",       sa.Text,       nullable=True),
        sa.Column("timestamp",   sa.DateTime,   nullable=False),
    )
    op.create_index("ix_pap_log_entity_type", "audit_pap_log", ["entity_type"])
    op.create_index("ix_pap_log_entity_id",   "audit_pap_log", ["entity_id"])
    op.create_index("ix_pap_log_action",      "audit_pap_log", ["action"])
    op.create_index("ix_pap_log_timestamp",   "audit_pap_log", ["timestamp"])

    # ── 11. audit_evaluation_audit ────────────────────────────────────────────
    op.create_table(
        "audit_evaluation_audit",
        sa.Column("id",                sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("interaction_id",    sa.String(36), nullable=False, unique=True),
        sa.Column("request_id",        sa.String(128), nullable=True),
        sa.Column("app_id",            sa.String(128), nullable=True),
        sa.Column("session_id",        sa.String(128), nullable=True),
        sa.Column("agent_id",          sa.String(128), nullable=True),
        sa.Column("turn_number",       sa.Integer,    nullable=True),
        sa.Column("role",              sa.String(32), nullable=False),
        sa.Column("verdict",           sa.String(16), nullable=False),
        sa.Column("violations",        sa.JSON,       nullable=True),
        sa.Column("remediations",      sa.JSON,       nullable=True),
        sa.Column("fail_reason",       sa.String(64), nullable=True),
        sa.Column("session_verdict",   sa.String(16), nullable=True),
        sa.Column("session_escalated", sa.Boolean,    nullable=False, server_default=sa.false()),
        sa.Column("latency_ms",        sa.Float,      nullable=True),
        sa.Column("enforcement_action", sa.String(16), nullable=True),
        sa.Column("metadata_json",     sa.JSON,       nullable=True),
        sa.Column("pack_trace",        sa.JSON,       nullable=True),
        sa.Column("timestamp",         sa.DateTime,   nullable=False),
    )
    op.create_index("ix_eval_audit_interaction_id", "audit_evaluation_audit", ["interaction_id"])
    op.create_index("ix_eval_audit_app_id",         "audit_evaluation_audit", ["app_id"])
    op.create_index("ix_eval_audit_session_id",     "audit_evaluation_audit", ["session_id"])
    op.create_index("ix_eval_audit_verdict",        "audit_evaluation_audit", ["verdict"])
    op.create_index("ix_eval_audit_timestamp",      "audit_evaluation_audit", ["timestamp"])

    # ── 12. audit_evaluation_dimensions (FK → audit_evaluation_audit) ─────────
    op.create_table(
        "audit_evaluation_dimensions",
        sa.Column("id",                  sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("evaluation_audit_id", sa.Integer,    sa.ForeignKey("audit_evaluation_audit.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dimension_id",        sa.String(64), nullable=False),   # soft ref — no FK
        sa.Column("score",               sa.Integer,    nullable=True),
        sa.Column("status",              sa.String(16), nullable=False),
        sa.Column("provider",            sa.String(64), nullable=True),
    )
    op.create_index("ix_eval_dim_audit_id", "audit_evaluation_dimensions", ["evaluation_audit_id"])


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("audit_evaluation_dimensions")
    op.drop_table("audit_evaluation_audit")
    op.drop_table("audit_pap_log")
    op.drop_table("config_session_policy")
    op.drop_table("config_policies")
    op.drop_table("config_dimension_overrides")
    op.drop_table("config_app_pack_assignments")
    op.drop_table("config_apps")
    op.drop_table("config_pack_dimensions")
    op.drop_table("config_policy_packs")
    op.drop_table("config_threshold_profiles")
    op.drop_table("config_evaluation_dimensions")
